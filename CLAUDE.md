# CLAUDE.md

**Port:** 8000 | **Stack:** Python 3.12, FastAPI, pydantic-settings, httpx, PyNaCl | **Image:** `ghcr.io/pocket-nutrition/recognition`

## What this service does

The recognition server receives an **audio clip or a photo of a meal** from the mobile apps and turns it into a **ticket**. The mobile app keeps the ticket id, polls (or long-polls) it, and when the ticket is `done` imports the extracted ingredient candidates. The server processes nothing itself (no speech-to-text, no image analysis, no extraction): it authenticates, queues, tracks tickets and hands jobs to a worker.

**Two pipelines** (`RECOGNITION_PIPELINE`; the phone-facing contract is identical in both):

- `worker`: what a deployed server runs (`.env.example` sets it). The clip waits in an in-memory queue and a **remote worker pulls it** ([`recognition-worker-osx`](https://github.com/pocket-nutrition/recognition-worker-osx), runs on a Mac you control: Handy/Cohere speech-to-text, Gemma extraction, public ingredient search). The server refuses to start without `RECOGNITION_WORKER_KEY` (16+ characters, different from `RECOGNITION_API_KEY`).
- `simulator`: development and tests only. Every clip yields the same static result (`app/simulator.py`) after a delay. It is the built-in default when `RECOGNITION_PIPELINE` is unset.

## Layout

```
app/main.py        create_app() / build(): settings, store, vault deliverer, sweeper; /health; worker router only in worker mode
app/config.py      Settings (RECOGNITION_* env) and the startup checks: some auth required, worker key rules
app/router.py      phone-facing routes (POST/GET/DELETE /captures), ticket-secret and drop-token auth
app/worker.py      /worker/* routes (claim, audio, progress, complete, fail) with their own key
app/store.py       TicketStore: in-memory tickets, tombstones, leases, long polling (asyncio.Condition)
app/vault.py       vault drops: https + public-address check, sealing to the phone's key, PUT /v1/drop, GET /v1/me
app/schemas.py     Status, TicketOut / TicketCreated, CaptureResult
app/simulator.py   the fixed sample result of the simulator pipeline
tests/             pytest, asyncio auto mode: captures, vault auth, vault delivery, worker queue
```

## Contract

What the mobile apps call (`app/router.py`, `app/schemas.py`; `/health` is in `app/main.py`), the same in both pipelines. Who may call it: see Authentication.

| Route | Behaviour |
|-------|-----------|
| `GET /health` | `{"status": "ok"}`, no credentials. |
| `POST /captures` (multipart `audio`: `audio/*`, or a photo `image/jpeg`/`image/png`, ≤ 10 MB) | `mode=async` (default) → 202 + ticket. `mode=sync` → waits ≤ 30 s, 200 if terminal else 202. `Location` header set. POC-only form fields (simulator, ignored in worker mode): `simulate_delay_s`, `simulate_outcome` (`done`/`failed`). |
| `GET /captures/{id}?wait=N&known_status=S` | Plain read, or long poll: blocks (N ≤ 25 s) until status ≠ `known_status` (default: status at call time) or terminal. |
| `DELETE /captures/{id}` | 204. |

A ticket is `{id, status, created_at, updated_at, expires_at, result, error, delivery}` (times in epoch seconds); the `POST /captures` answer adds `secret`. `result` is `{transcript, language, items: [{raw_text, quantity, unit, cooking_method, candidates: [{ingredient_id, name, score}]}]}`. Items carry ranked `candidates` (search-result style): the app lets the user pick one per item.

**Vault delivery (optional):** `POST /captures` also accepts `vault_url`, `vault_token`, `vault_record_id`, `inbox_public_key` (all four or none). The outcome (`done` or `failed`) is sealed to the phone's X25519 key and deposited with `PUT {vault_url}/v1/drop` (see the "Drops" section of the protocol documentation in the [`vault`](https://github.com/pocket-nutrition/vault) repository): by this service in `simulator` mode, by the worker in `worker` mode. The ticket turns terminal only after delivery, so `done` + `delivery: "delivered"` means the data is already in the vault; the result is then dropped from the ticket. If delivery fails the ticket reports `delivery: "failed"` and keeps the result as a fallback.

Statuses: `queued → transcribing → extracting → done | failed` (the worker takes a photo straight from `queued` to `extracting`).
Errors: 401 bad key, 404 unknown id, 409 drop token already used, **410 expired** (TTL passed (default 48 h); tombstone kept 7 days so the app can tell "expired" from "never existed"), 413 too large, 415 neither audio nor JPEG/PNG, 422 bad form fields (partial or malformed vault set, `wait` above the cap), 503 + `Retry-After` (vault unreachable when checking a drop token, or worker queue full).

## Authentication

Two ways in, either is enough (`RECOGNITION_API_KEY` and/or `RECOGNITION_TRUSTED_VAULTS`; at least one must be set):

- **Vault drop token (hosted default, no key to type).** `POST /captures` carries the four vault fields. The vault host must be in `RECOGNITION_TRUSTED_VAULTS` (comma-separated hostnames), then the service asks that vault `GET /v1/me` with the drop token; it must answer `role: dropper` (an owner key or any other role is refused: only a token that cannot read may authenticate here). The call returns an account id and role, never data. Vault answers 5xx or does not answer → 503 + `Retry-After` (retry), anything else not valid → 401. **One drop token, one dictation**: the same token again returns the same ticket (the app retrying an upload whose answer it lost); once that ticket is gone the token is refused with 409.
- **Shared key (self-hosted / legacy).** `Authorization: Bearer <RECOGNITION_API_KEY>` works everywhere, as before.

`POST /captures` answers with a **ticket secret** (`secret`, shown only then). `GET`/`DELETE /captures/{id}` accept that secret as the Bearer, or the shared key. A live ticket with the wrong credential is 401; an unknown id is 404 whatever is presented, and an expired ticket is 410 for its own secret or the shared key (404 for anyone else), so neither leaks which ids exist. Secrets live in memory like the tickets (the tombstone keeps only a hash).

All routes except `/health` are authenticated this way (the worker routes have their own key, below).

## Worker routes

Only mounted in `worker` mode; `Authorization: Bearer <RECOGNITION_WORKER_KEY>`, a different secret from the phone key, min 16 chars; the phone key gets 401 there and vice versa. The worker only ever calls out.

| Route | Behaviour |
|-------|-----------|
| `POST /worker/claim?wait=N` | Oldest waiting job under a fresh **lease** (`lease_s`, default 120 s) → 200 `{lease_id, ticket_id, lease_expires_at, audio_content_type, drop}`; 204 when empty after `wait` (≤ 25 s). |
| `GET /worker/leases/{lease}/audio` | The clip or photo, with its original content type (the worker branches on `image/*`). |
| `POST /worker/leases/{lease}/progress` `{status: transcribing\|extracting}` | Sets the ticket status and extends the lease (heartbeat). |
| `POST /worker/leases/{lease}/complete` `{delivery, result?}` | `delivered` (worker sealed it into the vault itself) → no result allowed; `none` (no vault requested) or `failed` (deposit refused) → result required and kept on the ticket. |
| `POST /worker/leases/{lease}/fail` `{error}` | Ticket `failed`. |

Dead/unknown lease → 409. A lease that expires requeues the job (`max_attempts`, default 3, then the ticket fails with "worker did not finish the job"). If no worker is up, tickets simply stay `queued` until their TTL. Queue bounds: `max_queued` jobs and `max_queued_bytes` of audio, claimed jobs included; beyond that `POST /captures` answers 503 + `Retry-After`.

## Invariants

- **In-memory store, single replica.** Tickets live in memory and die with the process (restart, redeploy, crash); the mobile app must treat 404 as "gone". Do not run more than one replica without moving the store to a shared one such as Redis (long polling uses an in-process `asyncio.Condition`).
- Audio (and photos, same path) is never written to disk. In `simulator` mode it is dropped immediately; in `worker` mode it sits in memory until the job finishes, fails, is deleted, or its ticket expires (then it is released with the ticket).
- **In worker mode the vault drop is deposited by the worker, not by this service**; the service only learns `delivered`, never the content. The drop token is passed to the worker in the claim response, so keep it write-only and short-lived (the phone mints it per capture).
- No shared datastores: this service keeps everything in memory and never writes to the databases or search indexes of other Pocket Nutrition components.
- **The service must never hold a key that can read the vault.** The drop token is write-only and the payload is sealed to a public key; do not add a path that reads from the vault or keeps delivered results. Its only call besides the deposit is `GET /v1/me` with that same drop token.
- **Never expose whether a worker is connected.** The app pushes audio without knowing if anything can process it; the only signal is the queue-full 503. Do not add a presence endpoint or field.
- `vault_url` is user-supplied: it must stay https + public-address only (`app/vault.py`), no redirects. Never loosen this outside tests.
- Keep `max_wait_s` ≤ 25 s: reverse proxies and CDNs commonly cut idle HTTP connections at around 100 s, and mobile networks earlier.
- **The simulator is a development and test aid.** A deployed server runs in `worker` mode: never document or configure the simulator as a way to run the service.

## Dev

```bash
./setup.sh              # or by hand: python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
RECOGNITION_PIPELINE=simulator RECOGNITION_API_KEY=dev .venv/bin/uvicorn app.main:build --factory --port 8000
```

`./setup.sh` also copies `.env.example` to `.env`. Config is env-driven (`RECOGNITION_*`: see `app/config.py`, and `.env.example` for the full list; its values are the defaults, except `RECOGNITION_PIPELINE`, which is `worker` there while the code falls back to `simulator`). The server does not load a `.env` file itself: export the variables, or pass the file to Docker with `--env-file`. Worker mode: `RECOGNITION_PIPELINE=worker RECOGNITION_WORKER_KEY=<random>`. Run pytest in a shell with no `RECOGNITION_*` exported: the tests rely on the defaults, and `RECOGNITION_PIPELINE=worker` in the environment breaks most of them.

## Deployment

Container image: `ghcr.io/pocket-nutrition/recognition` (built from the `Dockerfile`; listens on port 8000 and runs as a non-root user). Run **one replica** (see Invariants) behind a TLS-terminating reverse proxy and configure it with the `RECOGNITION_*` variables from `.env.example`. A deployed server runs in worker mode: `RECOGNITION_PIPELINE=worker`, `RECOGNITION_WORKER_KEY` (16+ characters, different from `RECOGNITION_API_KEY`), and at least `RECOGNITION_API_KEY` and/or `RECOGNITION_TRUSTED_VAULTS`, or the server refuses to start. The worker itself is [`recognition-worker-osx`](https://github.com/pocket-nutrition/recognition-worker-osx). The instance used by the public apps is `https://recognition.pocketnutrition.org`. No deployment manifests are included in this repository.

## Docs and contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR workflow, CI (`.github/workflows/ci.yml`) and CalVer releases.

# CLAUDE.md

**Port:** 8000 | **Stack:** Python 3.15, FastAPI, pydantic-settings, httpx, PyNaCl | **Image:** `ghcr.io/pocket-nutrition/recognition`

## What this service does

The recognition server is a **queue**: it receives an **audio clip or a photo of a meal** from the mobile apps, turns it into a **ticket**, and hands the upload to a **remote worker that pulls it** ([`recognition-worker-osx`](https://github.com/pocket-nutrition/recognition-worker-osx), runs on a Mac you control: Handy/Cohere speech-to-text, Gemma extraction, public ingredient search). It processes nothing itself (no speech-to-text, no image analysis, no extraction): it authenticates, queues, tracks tickets and hands jobs to the worker. The server refuses to start without `RECOGNITION_WORKER_KEY` (16+ characters, different from `RECOGNITION_API_KEY`).

**Results never pass through this service.** The worker seals the result to the phone's inbox key and deposits it in the user's [vault](https://github.com/pocket-nutrition/vault); the ticket only carries a status. When the ticket is `done` (`delivery: "delivered"`), the phone collects the result from its vault and imports the ingredient candidates. Without a worker, tickets stay `queued`.

## Layout

```
app/main.py        create_app() / build(): settings, store, sweeper; /health; phone and worker routers
app/config.py      Settings (RECOGNITION_* env) and the startup checks: some auth required, worker key rules
app/router.py      phone-facing routes (POST/GET/DELETE /captures), ticket-secret and drop-token auth
app/worker.py      /worker/* routes (claim, audio, progress, complete, fail) with their own key
app/store.py       TicketStore: in-memory tickets, tombstones, leases, long polling (asyncio.Condition)
app/vault.py       vault drop targets: https + public-address check, drop-token check with GET /v1/me
app/schemas.py     Status, TicketOut / TicketCreated
tests/             pytest, asyncio auto mode: captures, vault auth, worker queue
```

## Contract

What the mobile apps call (`app/router.py`, `app/schemas.py`; `/health` is in `app/main.py`). Who may call it: see Authentication.

| Route | Behaviour |
|-------|-----------|
| `GET /health` | `{"status": "ok"}`, no credentials. |
| `POST /captures` (multipart `audio`: `audio/*`, or a photo `image/jpeg`/`image/png`, ≤ 10 MB, plus the four vault fields) | `mode=async` (default) → 202 + ticket. `mode=sync` → waits ≤ 30 s, 200 if terminal else 202. `Location` header set. |
| `GET /captures/{id}?wait=N&known_status=S` | Plain read, or long poll: blocks (N ≤ 25 s) until status ≠ `known_status` (default: status at call time) or terminal. |
| `DELETE /captures/{id}` | 204. |

A ticket is `{id, status, created_at, updated_at, expires_at, error, delivery}` (times in epoch seconds); the `POST /captures` answer adds `secret`. It never carries a result. The result, in the vault, is `{transcript, language, items: [{raw_text, quantity, unit, cooking_method, candidates: [{ingredient_id, name, score}]}]}`; items carry ranked `candidates` (search-result style) and the app lets the user pick one per item.

**Vault drop target (required):** every `POST /captures` carries `vault_url`, `vault_token` (write-only drop token), `vault_record_id`, `inbox_public_key`; a missing or malformed one is 422, shared key or not. They are handed to the worker, which seals the outcome to that key and deposits it with `PUT {vault_url}/v1/drop` (see the "Drops" section of the protocol documentation in the [`vault`](https://github.com/pocket-nutrition/vault) repository). The ticket turns `done` only after the deposit, so `done` always means `delivery: "delivered"`: the data is already in the vault. A deposit that fails ends the ticket `failed`.

Statuses: `queued → transcribing → extracting → done | failed` (the worker takes a photo straight from `queued` to `extracting`).
Errors: 401 bad key, 404 unknown id, 409 drop token already used, **410 expired** (TTL passed (default 48 h); tombstone kept 7 days so the app can tell "expired" from "never existed"), 413 too large, 415 neither audio nor JPEG/PNG, 422 bad form fields (missing or malformed vault fields, `wait` above the cap), 503 + `Retry-After` (vault unreachable when checking a drop token, or worker queue full).

## Authentication

Two ways in, either is enough (`RECOGNITION_API_KEY` and/or `RECOGNITION_TRUSTED_VAULTS`; at least one must be set):

- **Vault drop token (hosted default, no key to type).** `POST /captures` carries the four vault fields. The vault host must be in `RECOGNITION_TRUSTED_VAULTS` (comma-separated hostnames), then the service asks that vault `GET /v1/me` with the drop token; it must answer `role: dropper` (an owner key or any other role is refused: only a token that cannot read may authenticate here). The call returns an account id and role, never data. Vault answers 5xx or does not answer → 503 + `Retry-After` (retry), anything else not valid → 401. **One drop token, one dictation**: the same token again returns the same ticket (the app retrying an upload whose answer it lost); once that ticket is gone the token is refused with 409.
- **Shared key (self-hosted).** `Authorization: Bearer <RECOGNITION_API_KEY>` works everywhere. The four vault fields are still required: they say where the result goes.

`POST /captures` answers with a **ticket secret** (`secret`, shown only then). `GET`/`DELETE /captures/{id}` accept that secret as the Bearer, or the shared key. A live ticket with the wrong credential is 401; an unknown id is 404 whatever is presented, and an expired ticket is 410 for its own secret or the shared key (404 for anyone else), so neither leaks which ids exist. Secrets live in memory like the tickets (the tombstone keeps only a hash).

All routes except `/health` are authenticated this way (the worker routes have their own key, below).

## Worker routes

`Authorization: Bearer <RECOGNITION_WORKER_KEY>`, a different secret from the phone key, min 16 chars, always required; the phone key gets 401 there and vice versa. The worker only ever calls out.

| Route | Behaviour |
|-------|-----------|
| `POST /worker/claim?wait=N` | Oldest waiting job under a fresh **lease** (`lease_s`, default 120 s) → 200 `{lease_id, ticket_id, lease_expires_at, audio_content_type, drop}`; 204 when empty after `wait` (≤ 25 s). |
| `GET /worker/leases/{lease}/audio` | The clip or photo, with its original content type (the worker branches on `image/*`). |
| `POST /worker/leases/{lease}/progress` `{status: transcribing\|extracting}` | Sets the ticket status and extends the lease (heartbeat). |
| `POST /worker/leases/{lease}/complete` `{"delivery": "delivered"}` | The sealed result is in the vault: ticket `done`. That exact body only; any other field (a `result` above all) is 422. |
| `POST /worker/leases/{lease}/fail` `{error}` | Ticket `failed`. |

Dead/unknown lease → 409. A lease that expires requeues the job (`max_attempts`, default 3, then the ticket fails with "worker did not finish the job"). If no worker is up, tickets simply stay `queued` until their TTL. Queue bounds: `max_queued` jobs and `max_queued_bytes` of audio, claimed jobs included; beyond that `POST /captures` answers 503 + `Retry-After`.

## Invariants

- **In-memory store, single replica.** Tickets live in memory and die with the process (restart, redeploy, crash); the mobile app must treat 404 as "gone". Do not run more than one replica without moving the store to a shared one such as Redis (long polling uses an in-process `asyncio.Condition`).
- Audio (and photos, same path) is never written to disk. It sits in memory until the job finishes, fails, is deleted, or its ticket expires (then it is released with the ticket).
- **This service never holds, receives or stores a result.** The worker deposits the sealed drop; the service only learns `delivered`. Do not add a route or field through which a result could come back. The drop token is passed to the worker in the claim response, so keep it write-only and short-lived (the phone mints it per capture).
- No shared datastores: this service keeps everything in memory and never writes to the databases or search indexes of other Pocket Nutrition components.
- **The service must never hold a key that can read the vault.** The drop token is write-only and the payload is sealed to a public key; do not add a path that reads from the vault. Its only call to a vault is `GET /v1/me` with that same drop token.
- **Never expose whether a worker is connected.** The app pushes audio without knowing if anything can process it; the only signal is the queue-full 503. Do not add a presence endpoint or field.
- `vault_url` is user-supplied: it must stay https + public-address only (`app/vault.py`), no redirects. Never loosen this outside tests.
- Keep `max_wait_s` ≤ 25 s: reverse proxies and CDNs commonly cut idle HTTP connections at around 100 s, and mobile networks earlier.

## Dev

```bash
./setup.sh              # or by hand: python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
RECOGNITION_API_KEY=dev RECOGNITION_WORKER_KEY=dev-worker-key-0123456789 .venv/bin/uvicorn app.main:build --factory --port 8000
```

`./setup.sh` also copies `.env.example` to `.env`. Config is env-driven (`RECOGNITION_*`: see `app/config.py`, and `.env.example` for the full list). The server does not load a `.env` file itself: export the variables, or pass the file to Docker with `--env-file`. `RECOGNITION_WORKER_KEY` (16+ characters, different from the API key) is always required; a leftover `RECOGNITION_PIPELINE` is ignored. Run pytest in a shell with no `RECOGNITION_*` exported: the tests rely on the defaults.

## Deployment

Container image: `ghcr.io/pocket-nutrition/recognition` (built from the `Dockerfile`; listens on port 8000 and runs as a non-root user). Run **one replica** (see Invariants) behind a TLS-terminating reverse proxy and configure it with the `RECOGNITION_*` variables from `.env.example`. It needs `RECOGNITION_WORKER_KEY` (16+ characters, different from `RECOGNITION_API_KEY`), and at least `RECOGNITION_API_KEY` and/or `RECOGNITION_TRUSTED_VAULTS`, or the server refuses to start. The worker itself is [`recognition-worker-osx`](https://github.com/pocket-nutrition/recognition-worker-osx). The instance used by the public apps is `https://recognition.pocketnutrition.org`. No deployment manifests are included in this repository.

## Docs and contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR workflow, CI (`.github/workflows/ci.yml`) and CalVer releases.

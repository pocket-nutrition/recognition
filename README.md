# Pocket Nutrition — Recognition Server

Meal recognition server for [Pocket Nutrition](https://github.com/pocket-nutrition): queues voice recordings and plate photos in memory until a worker processes them.

The mobile apps ([iOS](https://github.com/pocket-nutrition/ios), [Android](https://github.com/pocket-nutrition/android)) upload a voice recording or a photo of a meal, get a **ticket** back, poll it until the job is done, then collect the extracted ingredient candidates from their [vault](https://github.com/pocket-nutrition/vault).

**This server does not process anything itself, and results never pass through it.** It does no speech-to-text, no photo analysis and no ingredient extraction. It authenticates the upload, holds it in memory, hands it to a worker and keeps track of the ticket's status. The processing happens in the worker, and the result goes straight from the worker to the user's vault.

## Where it fits

1. The app uploads the recording or photo to `POST /captures`, together with its vault drop target (the vault URL, a write-only drop token, a record id and the phone's inbox public key), and gets a ticket.
2. The job waits in an in-memory queue until a worker pulls it. The worker is a separate project, [`recognition-worker-osx`](https://github.com/pocket-nutrition/recognition-worker-osx), a process that runs on a Mac you control. It only makes outbound calls, so nothing on the Mac is exposed to the internet. It runs speech-to-text or photo analysis and extracts the ingredients.
3. The worker seals the result to the phone's inbox public key, deposits it in the user's vault and tells this server it is delivered. The server only records the status: it never receives the result.
4. The app polls the ticket (long polling is supported). Once its status is `done`, the result is already in the vault: the app collects it from there and imports the ingredient candidates.

Tickets live in memory only and expire after 48 hours by default. A restart drops all of them, and the apps treat a 404 as "gone". Until a worker connects, uploads simply stay `queued`.

[`CLAUDE.md`](CLAUDE.md) has the full contract (routes, authentication, statuses, invariants).

## Prerequisites

- Python 3.12 or later for local development (`./setup.sh` uses `python3.12`, and CI runs the tests on 3.12; the container image in the [Dockerfile](Dockerfile) uses `python:3.15-slim`)
- `pip`
- Docker, only if you run the container image

## Quick start

```bash
git clone https://github.com/pocket-nutrition/recognition.git
cd recognition
./setup.sh                  # creates .venv, installs dependencies, copies .env.example to .env
```

Then fill in **both keys** in `.env` (`openssl rand -hex 32` makes a good one): `RECOGNITION_API_KEY` and `RECOGNITION_WORKER_KEY`. The server refuses to start without a valid worker key. See [Configuration](#configuration).

Start the server from a subshell, so the variables stay out of your terminal, and check it:

```bash
(set -a; . ./.env; set +a; exec .venv/bin/uvicorn app.main:build --factory --port 8000)
```

```bash
curl localhost:8000/health        # {"status":"ok"}
```

Then start a worker against it, with the same worker key: see [`recognition-worker-osx`](https://github.com/pocket-nutrition/recognition-worker-osx).

### Trying the API

Every upload names the vault drop its result goes to: an `https` vault on a public address, a write-only drop token minted by that vault, the record id the result is stored under, and the phone's inbox public key (base64 X25519). A missing or malformed field is refused with 422, with or without the API key.

With a server started on throwaway keys:

```bash
RECOGNITION_API_KEY=dev RECOGNITION_WORKER_KEY=dev-worker-key-0123456789 .venv/bin/uvicorn app.main:build --factory --reload --port 8000
```

upload a recording, then long-poll its ticket until a worker has delivered the result to the vault:

```bash
curl -H 'Authorization: Bearer dev' -F "audio=@meal.m4a;type=audio/mp4" \
  -F vault_url=https://vault.example -F vault_token=<drop token> \
  -F vault_record_id=<record id> -F inbox_public_key=<base64 X25519> \
  localhost:8000/captures
curl -H 'Authorization: Bearer dev' "localhost:8000/captures/<id>?wait=20&known_status=queued"
```

`dev` and `dev-worker-key-0123456789` are only throwaway keys for trying things locally; use long random ones anywhere else.

## Configuration

Every setting is an environment variable prefixed with `RECOGNITION_`. [`.env.example`](.env.example) lists all of them with a one-line description.

```bash
cp .env.example .env    # then edit
```

The server does not read `.env` by itself: export the variables (`set -a; . ./.env; set +a`) or give the file to Docker (`--env-file .env`).

Fill in both keys before the first start:

- `RECOGNITION_API_KEY`: a shared key, sent as `Authorization: Bearer <key>`. The simplest way to authenticate when you self-host.
- `RECOGNITION_WORKER_KEY`: the secret the worker presents on the `/worker/*` routes, 16 characters or more and different from the API key. The server refuses to start without it.

Instead of an API key you can set `RECOGNITION_TRUSTED_VAULTS`: comma-separated hostnames of vaults whose write-only drop tokens authenticate a capture, so the apps need no key. The instance used by the public apps is `https://recognition.pocketnutrition.org`. At least one of the two is required, or the server refuses to start.

Run a single replica: tickets are held in memory and are lost when the process stops.

## Container image

```bash
# .env is .env.example with both keys filled in (see Configuration)
docker run -d -p 8000:8000 --env-file .env ghcr.io/pocket-nutrition/recognition
```

CI publishes the image to `ghcr.io/pocket-nutrition/recognition`, tagged with the CalVer release (for example `2026.11.1`), `latest` and the commit (`sha-<commit>`). It listens on port 8000 and runs as a non-root user. Put the container behind a TLS-terminating reverse proxy: keys and tokens travel in request headers.

To build it yourself:

```bash
docker build -t recognition .
docker run --rm -p 8000:8000 --env-file .env recognition
```

## Tests

```bash
source .venv/bin/activate     # created by ./setup.sh
pytest
```

Run the tests in a shell where no `RECOGNITION_*` variable is exported: they rely on the built-in defaults, and exported variables change them.

## API at a glance

| Route | Purpose |
|-------|---------|
| `GET /health` | Liveness, no credentials |
| `POST /captures` | Upload a recording or a photo (multipart field `audio`) with the four vault fields; answers with a ticket and its secret |
| `GET /captures/{id}` | Read a ticket's status (never a result); `?wait=N&known_status=S` long-polls |
| `DELETE /captures/{id}` | Delete a ticket |
| `/worker/*` | With the worker key only: claim a job, download it, report progress, report it delivered or failed |

## Using with Claude Code

This project includes a [CLAUDE.md](CLAUDE.md) that gives Claude Code full context on the contract, authentication, worker routes and invariants of the service.

```bash
claude    # Start Claude Code — reads CLAUDE.md automatically
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing expectations, and the PR workflow.

## License

AGPL-3.0 — see [LICENSE](LICENSE).

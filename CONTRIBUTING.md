# Contributing to the Pocket Nutrition recognition server

Thanks for your interest in improving this service. It is a small FastAPI server that queues voice
recordings and meal photos until a worker processes them, so the contribution process is intentionally
lightweight.

## Development setup

```bash
git clone https://github.com/pocket-nutrition/recognition.git
cd recognition
./setup.sh                       # creates .venv, installs dependencies, copies .env.example → .env
source .venv/bin/activate
```

Or set up manually:

```bash
python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements-dev.txt

cp .env.example .env             # then fill in RECOGNITION_API_KEY and RECOGNITION_WORKER_KEY
```

Run the service with the simulator pipeline. It returns the same fixed sample result for every upload, so
you can work on the API and on clients without a worker (development and tests only):

```bash
RECOGNITION_PIPELINE=simulator RECOGNITION_API_KEY=dev uvicorn app.main:build --factory --reload --port 8000
```

To work on the worker side, run in worker mode, which is how a deployed server runs. The worker key must be
16 characters or more and different from the API key, otherwise the server refuses to start:

```bash
RECOGNITION_PIPELINE=worker RECOGNITION_API_KEY=dev RECOGNITION_WORKER_KEY=dev-worker-key-0123456789 \
  uvicorn app.main:build --factory --reload --port 8000
```

Then connect [`recognition-worker-osx`](https://github.com/pocket-nutrition/recognition-worker-osx), or play the
worker by hand: upload a file to `POST /captures`, then
`curl -X POST -H 'Authorization: Bearer dev-worker-key-0123456789' 'localhost:8000/worker/claim?wait=5'`
hands you the job. The "Worker routes" section of [CLAUDE.md](CLAUDE.md) lists the rest of the calls.

The server does not read `.env` by itself. To use it, load it in a subshell so the variables do not leak
into your terminal: `(set -a; . ./.env; set +a; exec uvicorn app.main:build --factory --port 8000)`.

## Running tests

```bash
pytest -v
```

Run the tests in a shell where no `RECOGNITION_*` variable is exported. The tests rely on the built-in
defaults, and exported variables change them (for example `RECOGNITION_PIPELINE=worker` makes most of the
suite fail).

There is no code coverage gate or linter configuration in this repository: keep changes consistent with the
surrounding style (type hints on function signatures, English identifiers and comments, a comment where the
reason for a choice is not obvious).

| File | Covers |
|------|--------|
| `tests/test_captures.py` | Ticket lifecycle on the simulator pipeline: statuses, long polling, sync mode, expiry, deletion |
| `tests/test_vault_auth.py` | Authentication with drop tokens and ticket secrets, startup checks on the settings |
| `tests/test_vault_delivery.py` | Sealing and depositing results into a vault, SSRF protection |
| `tests/test_worker_queue.py` | Worker mode: the queue, leases, retries and the `/worker/*` routes |

## Test-first expectations

This service decides who may read which ticket and what leaves the process, so small changes can have
security consequences. For any change to `app/router.py`, `app/store.py`, `app/vault.py` or `app/worker.py`:

1. Add or update a test in `tests/` that captures the behavior you are adding or fixing.
2. Confirm the test fails without your change and passes with it.
3. Run the full suite (`pytest -v`) before opening a pull request. It must pass.

Pull requests that change behavior without an accompanying test will be asked to add one.

## Invariants

Read the "Invariants" section of [CLAUDE.md](CLAUDE.md) before touching the store, the vault code or the
routes. In short:

- Tickets and recordings live in memory only, in a single replica. Nothing is written to disk or to a database.
- The service never holds a key that can read the vault. It only deposits sealed payloads with write-only
  drop tokens.
- Nothing may reveal whether a worker is connected.
- `vault_url` comes from a phone: it stays https and public-address only, with no redirects.
- A deployed server runs in worker mode. The simulator is a development and test aid, never a way to run
  the service.

## Branch and PR workflow

1. Fork the repository and create a feature branch off `main`.
2. Make focused commits with clear messages (e.g. `fix: refuse a drop token whose ticket is gone`).
3. Ensure `pytest -v` passes locally.
4. Open a pull request against `main` and fill in the template: what changes and why, and which tests cover it.
5. Be responsive to review feedback. Small, focused PRs are easier to review and merge.

## Reporting issues

Please open a GitHub issue using the bug report template. Include the request you sent (the route and form
fields, not the file), the status code and body you got back, the pipeline (`worker` or `simulator`) and the
version you run. Tickets live in memory and expire after 48 hours by default, so a ticket that is gone after a
restart is expected (404), not a bug.

Redact API keys, worker keys, drop tokens and ticket secrets before pasting anything, and do not attach real
meal recordings or photos.

## CI and releases

- Every pull request runs `.github/workflows/ci.yml`: the tests on Python 3.12 and a check that the container
  image builds. The `ci-success` check must be green before a pull request can be merged, the branch must be
  up to date with `main`, and pull requests are squash-merged so history stays linear.
- If you contribute from a fork, the workflow waits for a maintainer to approve it. It runs with a
  read-only token and no secrets.
- Every commit merged to `main` is released automatically with a CalVer version, `vYYYY.M.N`: the year, the
  month, then a counter that restarts at 1 each month (for example `v2026.11.1`). You never pick a version
  number or edit a changelog; release notes are generated from the merged pull requests.
- A release publishes the container image `ghcr.io/pocket-nutrition/recognition`.

## Using Claude Code

This repository includes a [CLAUDE.md](CLAUDE.md) with the architecture, the contract and the invariants.

```bash
claude    # Start Claude Code in the repository root: it reads CLAUDE.md automatically
```

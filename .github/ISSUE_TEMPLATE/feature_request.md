---
name: Feature request
about: Suggest an idea or improvement for this service
title: "[Feature] "
labels: enhancement
assignees: ''
---

## Is your feature request related to a problem?

A clear and concise description of what the problem is (e.g. "I'm always frustrated
when...").

## Describe the solution you'd like

A clear and concise description of what you want to happen. If it changes a route or the ticket
JSON (`app/router.py`, `app/worker.py`, `app/schemas.py`), include an example request and response.

## Describe alternatives you've considered

Any alternative solutions or features you've considered.

## Scope check

This service is a small queue and ticket store. The "Invariants" section of `CLAUDE.md` applies.

- [ ] This does not require the service to transcribe or analyse recordings or photos itself: that
      work belongs to the worker.
- [ ] This does not require writing recordings, photos or results to disk or to a database: tickets
      live in memory by design.
- [ ] This does not require a key that can read the vault: only write-only drop tokens are used.
- [ ] This does not reveal whether a worker is connected.

## Additional context

Add any other context, references, or screenshots about the feature request here.

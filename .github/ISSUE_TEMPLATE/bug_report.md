---
name: Bug report
about: Report unexpected behavior of the recognition server
title: "[Bug] "
labels: bug
assignees: ''
---

## Describe the bug

A clear and concise description of what is wrong.

## Steps to reproduce

The route you called and how, for example a `curl` command. Replace every key, token and ticket secret
with a placeholder.

```bash
curl -H 'Authorization: Bearer <redacted>' -F "audio=@meal.m4a;type=audio/mp4" https://your-server/captures
```

Do not attach real meal recordings or photos: reproduce with a synthetic file if you can.

## Actual response

The HTTP status, the headers that matter (`Retry-After`, `Location`) and the JSON body you received.

```json
```

## Expected behavior

What you expected instead, and why.

## Environment

- Service version: the image tag (for example `2026.11.1`) or `git rev-parse HEAD`
- Python version, if you run it from source: `python --version`
- How you ran it: `uvicorn` locally / Docker image / other
- Pipeline: `RECOGNITION_PIPELINE` is `worker` or `simulator`
- Worker version, if a worker is involved (`recognition-worker-osx`)
- Reverse proxy in front of the server, if any

## Additional context

Server logs, stack traces, and anything else that helps. Redact API keys, worker keys, drop tokens and
ticket secrets first.

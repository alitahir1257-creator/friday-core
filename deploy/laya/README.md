# Remote Laya System-1 service

This template runs the official `laya-serve` entry point from `laya[serve]==0.3.26` on CPU Linux. It preloads only the English checkpoint, limits the resident checkpoint count to one, and provides the official unauthenticated `GET /health` check. It does not copy or fork Laya code.

Protocol and release references: [official HTTP API](https://github.com/NandhaKishorM/laya/blob/main/docs/http-api.md) and [Laya 0.3.26 on PyPI](https://pypi.org/project/laya/0.3.26/).

## Start

Install Docker Engine with Compose on the remote Linux host. Provide a strong `LAYA_API_KEY` through that host's secret/environment mechanism, then run from this directory:

```sh
docker compose up --build -d
docker compose ps
```

The Compose port is bound to `127.0.0.1:8000` on the host. Put a TLS reverse proxy in front of it or expose it through a private tunnel; do not publish the bearer-authenticated service over plain HTTP. The health endpoint remains open by design. The first startup downloads the selected checkpoint on the remote host.

Set FRIDAY's `FRIDAY_LAYA_ENABLED=true`, `FRIDAY_LAYA_BASE_URL=https://your-private-laya.example`, and `FRIDAY_LAYA_API_KEY` in FRIDAY's environment after you have a verified zero-cost host and secure HTTPS route. `FRIDAY_LAYA_TIMEOUT_SECONDS` defaults to 5. FRIDAY sends only an eligible current command to `/v1/systemone`, never the conversation history.

This repository does not provision or choose a hosting provider. Only run it on infrastructure whose no-cost terms and resource limits you have verified; Compose itself does not guarantee free hosting.

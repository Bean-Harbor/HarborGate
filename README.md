# HarborGate

HarborGate is the Rust-based IM transport gateway for HarborBeacon.

The active IM service-to-service contract is
[`HarborBeacon-HarborGate-Agent-Contract-v2.0.md`](./HarborBeacon-HarborGate-Agent-Contract-v2.0.md).
The northbound channel-edge upgrade is
[`HarborBeacon-HarborGate-Agent-Contract-v3.0.md`](./HarborBeacon-HarborGate-Agent-Contract-v3.0.md).
HarborGate owns IM adapters, channel-edge entrypoints, platform credentials,
setup/admin pages, inbound normalization, route registry, outbound delivery,
and redacted gateway status.
HarborBeacon owns business conversation state, active frames, approvals,
artifacts, audit, and local model policy.

## Runtime

Rust is the only current runtime on main. The historical Python runtime was
retired from main after the archive tag:

```text
archive/harborgate-python-runtime-final-20260501
```

Rollback is handled by installing a previously verified release artifact, not by
switching a Python fallback inside the current release.

## Quick Start

```powershell
just test
just start
```

The service listens on `127.0.0.1:8787` by default.

Useful checks:

```powershell
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/api/setup/status
```

Release build:

```powershell
just build
```

Portable Linux release builds are produced on the builder with:

```bash
just build-linux
```

## Current Adapters

- `feishu`: websocket receive, webhook callback compatibility, text send,
  native image send, and interactive-card delivery path.
- `weixin`: QR login, account/session state, private-DM long polling, duplicate
  guard, context token cache, text send, native image send, and file/video
  upload delivery path.
- `webhook`: generic inbound route for controlled tests and integration probes.

Weixin group chat remains outside the current ready scope.

## HarborBeacon Boundary

HarborGate sends inbound IM turns to HarborBeacon:

```text
POST /api/web/turns
X-Contract-Version: 2.0
```

Android/Web clients enter through HarborGate:

```text
POST /api/gateway/turns
```

Beacon-owned admin/config APIs are proxied through HarborGate:

```text
/api/beacon/* -> HarborBeacon /api/*
```

Rules that must not drift:

- do not post active turns to `/api/tasks`
- do not emit `args.resume_token`
- do not interpret `active_frame.kind` for business routing
- do not import HarborBeacon runtime code
- do not store IM platform credentials in HarborBeacon

HarborGate exposes outbound delivery for HarborBeacon:

```text
POST /api/notifications/deliveries
```

## Public HTTP Surface

- `GET /health`
- `GET /api/setup/status`
- `GET /api/gateway/status`
- `POST /api/gateway/turns`
- `/api/beacon/*`
- `POST /messages/webhook`
- `POST /messages/feishu`
- `POST /messages/weixin`
- `POST /api/notifications/deliveries`
- `GET /setup/*`
- `GET /admin/im/*`
- `POST /api/setup/feishu/configure`
- `POST /api/setup/weixin/login/start`
- `GET /api/setup/weixin/login/status`
- `POST /api/setup/weixin/unbind`

Setup pages use customer-facing HarborOS styling and must not expose service
names, raw credentials, runtime file paths, or internal ports.

## Configuration

Core:

```text
IM_AGENT_HOST=127.0.0.1
IM_AGENT_PORT=8787
IM_AGENT_CONTRACT_VERSION=2.0
IM_AGENT_SERVICE_TOKEN=<shared-service-token>
HARBORBEACON_WEB_API_URL=http://127.0.0.1:4174
HARBORBEACON_WEB_API_TOKEN=<shared-service-token>
```

Feishu:

```text
FEISHU_APP_ID=<app-id>
FEISHU_APP_SECRET=<app-secret>
FEISHU_CONNECTION_MODE=websocket
FEISHU_ENABLE_LIVE_SEND=1
HARBORGATE_RUST_FEISHU_WEBSOCKET=1
```

Weixin:

```text
WEIXIN_STATE_DIR=<state-dir>
HARBORGATE_WEIXIN_RUNTIME_ENABLED=1
```

## Repository Layout

```text
Cargo.toml
rust/harborgate/
  Cargo.toml
  src/
    adapters/
    config.rs
    gateway.rs
    harborbeacon.rs
    runtime.rs
    server.rs
    setup.rs
    store.rs
```

## Verification

Local gate:

```powershell
just fmt
just test
just build
```

Release gate on the Linux builder:

```bash
just build-linux
```

Live acceptance:

- `harboros-im-gate.service` reports runtime `rust`
- Feishu private DM receives a reply
- Weixin private DM receives a reply
- `/api/setup/status` reports Feishu connected and Weixin polling/connected
- `/api/gateway/status` redacts sensitive transport state

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

The HarborNavi K3 package is built in the pinned container declared by
`.github/workflows/k3-evt-package.yml`. A qualification build uses an exact
Debian version and the source commit timestamp; it does not publish to a public
release or APT channel:

```bash
export DEBIAN_VERSION=0.1.0+harbornavi.k3.evt1.riscv64
export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
export SOURCE_COMMIT="$(git rev-parse HEAD)"
export HARBORGATE_BUILD_CONTAINER_DIGEST=sha256:a339861ae23e9abb272cea45dfafde21760d2ce6577a70f8a926153677902663
export HARBORGATE_DEBIAN_SNAPSHOT=20260801T000000Z
./scripts/verify_k3_reproducible.sh
```

The K3 service remains loopback-only and stores sessions and transport state
under `/data/harborgate`. HarborOS System provisions the shared service bearer;
the Gate package never generates or exports that cross-component secret. The
package depends on the compatible HarborOS System line and loads only the
required `/data/harboros/secrets/beacon-gate.env`; no `/etc/default` file may
override the K3 listener, data roots, contract, or bearer values. Startup fails
closed unless the listener is loopback `127.0.0.1:8787`, the contract is `2.0`,
all component state is under `/data/harborgate`, and both distinct 32-byte
bearers are present.

The repository CI proves an amd64 package install and loopback health smoke. It
cross-builds the riscv64 package but does not claim that QEMU or real K3 runtime
acceptance occurred. Each package therefore carries
`k3-runtime-evidence-required.json`; HarborOS qualification/candidate assembly
must fail closed until signed Ubuntu 26.04 riscv64 dependency-closure, install,
systemd ordering, binary-execution, and loopback-health evidence exists. The
HarborOS central release resolver derives `Depends`, `Pre-Depends`, and
`Provides` closure from the real `.deb`; this repository's Debian snapshot and
tool versions are build-material provenance, not a substitute for that Ubuntu
closure.

The bundle also records Harbor Innovations' first-party distribution approval
inside the deb and in a byte-identical sidecar. That approval covers Gate's
first-party source and brand materials for HarborNavi qualification only; it
does not infer licenses for locked Cargo dependencies. Until those third-party
materials are reviewed, `license-review.json` and the canonical
`<deb>.release-materials.json` remain `blocked`/`release_eligible=false`. The
full `<deb>.materials.sha256` manifest covers the final deb, descriptor, SBOMs,
provenance, rights evidence, component contract, and every other sidecar.

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
/api/harbor-gate/api/beacon/* -> HarborBeacon /api/*
```

Knowledge search and conversation JSON requests require a fresh 30-second
single-use HarborOS token in `X-HarborOS-Auth-Token`. HarborGate validates the
token through middleware, removes all client identity and authorization input,
and forwards a canonical HarborOS principal with the Gate-to-Beacon service
bearer. The proxy requires `HARBORBEACON_WEB_API_TOKEN` and never falls back to
the legacy IM `HARBORBEACON_TASK_API_TOKEN`. Browser clients must never receive
the service bearer.

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
- `/api/harbor-gate/api/beacon/*`
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
HARBOR_WORKSPACE_ID=home-1
```

Feishu:

```text
FEISHU_APP_ID=<app-id>
FEISHU_APP_SECRET=<app-secret>
FEISHU_CONNECTION_MODE=websocket
FEISHU_ENABLE_LIVE_SEND=1
HARBORGATE_RUST_FEISHU_WEBSOCKET=1
```

Feishu Mail delivery:

```text
FEISHU_MAIL_ENABLED=1
FEISHU_MAIL_SENDER_MAILBOX=me
FEISHU_MAIL_DEFAULT_FROM_NAME=HarborOps
FEISHU_MAIL_USER_ACCESS_TOKEN=<short-lived-user-token>
FEISHU_MAIL_USER_REFRESH_TOKEN=<rotating-user-refresh-token>
FEISHU_MAIL_TOKEN_STATE_PATH=/data/harborgate/feishu-mail-token.json
```

`FEISHU_MAIL_USER_ACCESS_TOKEN` is useful for one-off smoke delivery. For a
longer-lived HarborOps route, configure the refresh token and token state path;
Gate will refresh the user token through Feishu's OAuth v2 token endpoint using
the app credentials and persist the rotated token state. Treat
`FEISHU_MAIL_USER_REFRESH_TOKEN` as a bootstrap seed; after the first successful
refresh, the token state file is the source of truth for rotated refresh tokens.
Keep the state file private to the Gate host. Beacon and HarborOps must only
reference the Gate delivery route and must not store Feishu credentials or token
material.

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

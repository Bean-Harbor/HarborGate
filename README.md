# HarborGate

HarborGate is the Rust-based IM transport gateway for HarborBeacon.

The active IM service-to-service contract is
[`HarborBeacon-HarborGate-Agent-Contract-v2.0.md`](./docs/HarborBeacon-HarborGate-Agent-Contract-v2.0.md).
The northbound channel-edge upgrade is
[`HarborBeacon-HarborGate-Agent-Contract-v3.0.md`](./docs/HarborBeacon-HarborGate-Agent-Contract-v3.0.md).
`POST /api/gateway/turns` defaults to V3.0, accepts an explicit V2.0 contract
header during migration, and returns the negotiated version. The internal
Gate-to-Beacon turn call and notification delivery remain on V2.0.
HarborGate owns IM adapters, channel-edge entrypoints, platform credentials,
setup/admin pages, inbound normalization, route registry, outbound delivery,
and redacted gateway status.
HarborBeacon owns business conversation state, active frames, approvals,
artifacts, audit, and local model policy.

HarborCloud owns account, entitlement, Hub identity, WebRTC signaling, and cloud
metadata. HarborLink owns Hub-side outbound MQTT and Home Assistant/camera
bridge execution. harbor-dock owns Android/Paper UI intent. HarborNAS-webui
owns HarborOS UI presentation. HarborGate must not absorb those product
boundaries.

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
under `/data/harborgate`. Its unit selects `HARBORGATE_RUNTIME_PROFILE=k3`;
standard AMD64 packages retain the normal configurable runtime profile. K3
package templates live in `debian/harbornavi-k3`, and both package paths include
the directional service-credential writer and recovery unit. Installation runs
only `prepare`; credential switch, finalize and rollback remain explicit
operations. K3 loads its Gate-to-Beacon sender and current/previous
Beacon-to-Gate receivers through systemd `LoadCredential` from
`/etc/harboros/service-auth`, with recovery ordered before startup. It does not
load the old shared `/data/harboros/secrets/beacon-gate.env` or `/etc/default`
overrides. Startup fails closed unless its listener, data roots, v2.0 contract
and distinct credentials satisfy the K3 profile.

The earlier `scripts/build_harbornavi_k3_deb.sh` entrypoint forwards to the
audited builder. It accepts the existing `TARGET`, `VERSION`, and `OUT_DIR`
settings, while requiring the same explicit version and provenance environment
as the canonical build.

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
does not infer licenses for locked Cargo dependencies. The separate
`third-party-licenses.json` evidence enumerates the target-specific normal/build
closure from `cargo metadata`, verifies every downloaded `.crate` against its
`Cargo.lock` checksum, and embeds the package-local license text and hashes.
Development-only dependencies are excluded from the shipped target closure. A
missing declaration, archive, or package-local license text keeps
`license-review.json` and `<deb>.release-materials.json` fail-closed; a complete
closure makes that package-material decision `approved`/`release_eligible=true`.
This does not replace HarborOS's signed APT dependency-closure gate. The full
`<deb>.materials.sha256` manifest covers the final deb, descriptor, SBOMs,
provenance, rights evidence, component contract, third-party license evidence,
and every other sidecar.

## Current Adapters

- `feishu`: websocket receive, webhook callback compatibility, text send,
  native image send, and interactive-card delivery path.
- `weixin`: QR login, account/session state, private-DM long polling, duplicate
  guard, context token cache, text send, native image send, and file/video
  upload delivery path.
- `whatsapp`: Business Platform webhook verification, durable duplicate handling,
  text and JPEG/PNG image delivery. Camera images are downloaded through the
  authenticated Beacon media proxy before the adapter uploads them. This is a
  development candidate; a provisioned Meta number and real delivery are still
  required for production qualification.
- `webhook`: generic inbound route for controlled tests and integration probes.

Weixin group chat remains outside the current ready scope.

Camera delivery rechecks consent through Beacon before sending, after a new
provider upload, and when retrying a cached or previously uploaded attachment.
The request uses `X-Harbor-Media-Context: chat` and `Range: bytes=0-0`; the media
proxy URL permits only the optional fixed query `media_context=chat`. Redirects,
other queries, and foreign origins are rejected. Revocation or expired media
ends delivery; a temporary authorization-service outage remains retryable.
This preserves the existing artifact and turn/result envelopes. Gate does not
decide household permissions, and cannot recall copies already sent to a provider
or receiving chat app. One official number serving multiple Navi homes and the
remote return channel remain separate implementation work.

## HarborBeacon Boundary

HarborGate sends inbound IM turns to HarborBeacon:

```text
POST /api/web/turns
X-Contract-Version: 2.0
```

Android/Web assistant chat clients may enter through HarborGate:

```text
POST /api/gateway/turns
```

Beacon-owned admin/config APIs are proxied through HarborGate:

```text
/api/beacon/* -> HarborBeacon /api/*
/api/harbor-gate/api/beacon/* -> HarborBeacon /api/*
```

This proxy is not a cloud, Hub, or UI state owner. HarborDock remote home/camera
control remains a HarborCloud + HarborLink + harbor-dock concern unless an
approved assistant turn explicitly enters the Beacon/Gate seam.

Knowledge search and conversation JSON requests require a fresh 30-second
single-use HarborOS token in `X-HarborOS-Auth-Token`. HarborGate validates the
token through middleware, removes all client identity and authorization input,
and forwards a canonical HarborOS principal with the Gate-to-Beacon service
bearer. The proxy requires `HARBOR_GATE_TO_BEACON_TOKEN`; the legacy
`HARBORBEACON_WEB_API_TOKEN` name is accepted only during the RC migration and
never falls back to task or Beacon-to-Gate credentials. Browser clients must
never receive the service bearer.

Rules that must not drift:

- do not post active turns to `/api/tasks`
- do not emit `args.resume_token`
- do not interpret `active_frame.kind` for business routing
- do not import HarborBeacon runtime code
- do not store IM platform credentials in HarborBeacon
- do not route HarborCloud entitlement, HarborLink MQTT, HarborDock remote
  control, or WebUI display state through HarborGate business semantics

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
HARBOR_BEACON_TO_GATE_TOKEN=<inbound-current-token>
HARBOR_BEACON_TO_GATE_TOKEN_PREVIOUS=<inbound-rotation-token>
HARBORBEACON_WEB_API_URL=http://127.0.0.1:4174
HARBOR_GATE_TO_BEACON_TOKEN=<outbound-current-token>
HARBOR_WORKSPACE_ID=home-1
```

Packaged services receive these values through role-scoped systemd credentials.
See `docs/HarborGate-HarborBeacon-Service-Auth-Rotation-Runbook.md` for the
prepare, switch, finalize, and rollback order.

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

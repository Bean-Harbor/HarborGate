# HarborGate Rust-Only Plan

## Baseline

HarborGate main is Rust-only. The active implementation guide is
[`HarborBeacon-HarborGate-Agent-Contract-v2.0.md`](./HarborBeacon-HarborGate-Agent-Contract-v2.0.md).
The northbound channel-edge upgrade guide is
[`HarborBeacon-HarborGate-Agent-Contract-v3.0.md`](./HarborBeacon-HarborGate-Agent-Contract-v3.0.md).

The retired Python runtime is preserved only in Git history and the archive tag
`archive/harborgate-python-runtime-final-20260501`.

## Mission

Keep HarborGate as the IM transport boundary and northbound assistant/channel
edge for HarborBeacon:

- own Feishu, Weixin, webhook, setup/admin, route registry, delivery, and
  redacted gateway status
- own Android/Web assistant-channel turn entry and Beacon admin/config proxying
- call HarborBeacon only through HTTP/JSON
- keep business state, active-frame semantics, approvals, artifacts, and audit in
  HarborBeacon
- keep HarborCloud entitlement, HarborLink MQTT command/ack, HarborDock remote
  home/camera control, and WebUI display state outside HarborGate semantics

## Current Workstreams

1. Rust runtime hardening
   - keep `harborgate` as the only binary
   - keep adapter status redacted and customer-facing setup pages clean
   - maintain Feishu websocket and Weixin long-poll supervision in-process

2. Release integration
   - package only `harborgate/bin/harborgate`
   - do not vendor Python site-packages
   - rollback by installing an older verified release artifact
   - current HarborAssistant handoff id:
     `harborassistant-live-solidify-20260529`; the Gate deb is
     `harboros-im-gate_20260529+harborassistant.live.solidify_harborassistant-live-solidify-20260529_linux_amd64.deb`
     under `.197`
     `/home/harbor-innovations/artifacts/harborassistant-live-solidify-20260529/output`
   - Gate package acceptance for this handoff is setup/manage/status under
     `/api/harbor-gate/*`, explicit v2.0 runtime defaults, the
     `harboros-im-gate.service` unit, and no IM business semantics added to
     HarborAssistant or Beacon

3. Product acceptance
   - verify Harbor Assistant Messages tab against `/api/setup/status`
   - verify Harbor Assistant Search requests stay under `/api/beacon/*`
   - verify Android/Web turns enter through `POST /api/gateway/turns`
   - run Feishu and Weixin private-DM live acceptance before release

## Drift Guards

The project is not release-ready if active code:

- posts HarborBeacon turns to `/api/tasks`
- emits `args.resume_token`
- routes business behavior from `active_frame.kind`
- imports HarborBeacon runtime code
- persists Beacon-owned device credentials, model secrets, or camera config in Gate
- treats HarborCloud, HarborLink, HarborDock, or WebUI state as Gate-owned
  business truth
- reintroduces Python runtime packaging or `im_agent` entrypoints

## Verification

```powershell
cargo fmt --check
cargo test
cargo build --release --bin harborgate
```

Builder:

```bash
cargo zigbuild --release --bin harborgate --target x86_64-unknown-linux-musl
```

## Current Release Blocker

The `harborassistant-live-solidify-20260529` package lane is dry-verified but
not live-installed because build host `.197` cannot currently reach HarborOS
`.82`: ping drops, TCP `22/80/443/4174/8787` time out, and SSH jump to `.82:22`
fails. Live Gate acceptance resumes only after `.82` is reachable through `.197`
or another confirmed jump host.

Central install, rollback, and live-gate instructions live in
`C:\Users\beanw\OpenSource\HarborBeacon\docs\harbor-assistant-offline-delivery-runbook.md`.

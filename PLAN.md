# HarborGate Rust-Only Plan

## Baseline

HarborGate main is Rust-only. The active implementation guide is
[`HarborBeacon-HarborGate-Agent-Contract-v2.0.md`](./HarborBeacon-HarborGate-Agent-Contract-v2.0.md).

The retired Python runtime is preserved only in Git history and the archive tag
`archive/harborgate-python-runtime-final-20260501`.

## Mission

Keep HarborGate as the IM transport boundary for HarborBeacon:

- own Feishu, Weixin, webhook, setup/admin, route registry, delivery, and
  redacted gateway status
- call HarborBeacon only through HTTP/JSON
- keep business state, active-frame semantics, approvals, artifacts, and audit in
  HarborBeacon

## Current Workstreams

1. Rust runtime hardening
   - keep `harborgate` as the only binary
   - keep adapter status redacted and customer-facing setup pages clean
   - maintain Feishu websocket and Weixin long-poll supervision in-process

2. Release integration
   - package only `harborgate/bin/harborgate`
   - do not vendor Python site-packages
   - rollback by installing an older verified release artifact

3. Product acceptance
   - verify HarborDesk IM Connectors against `/api/setup/status`
   - verify HarborBot retrieval stays under `/api/harbordesk/*`
   - run Feishu and Weixin private-DM live acceptance before release

## Drift Guards

The project is not release-ready if active code:

- posts HarborBeacon turns to `/api/tasks`
- emits `args.resume_token`
- routes business behavior from `active_frame.kind`
- imports HarborBeacon runtime code
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

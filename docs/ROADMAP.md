# HarborGate Rust Roadmap

> 2026-09-12 receipt candidate: durable central route updates now return to Beacon and the setup browser. Owner confirmation alone does not advance remote setup; the matching active route receipt is required. Gate 155 library tests and actual Cloud asset interoperability pass; mobile production-bundle fixtures cover waiting, reload, switch/reconnect and skip. Consumer activation, remote media/notifications and real Meta remain unfinished.

> 2026-09-12 fleet candidate: [phone proof and endpoint selection](navi-whatsapp-binding-proof.md) now select the remote client for configured WhatsApp ingress. Pending switches pause old work; queue/history retain the originating selection and certificate identity. Gate 153 library and 12 HTTP tests pass. Consumer activation, browser central-selection acknowledgement, remote media/notifications and real Meta remain unfinished. All eight features precede PR/Main/IMG/OTA.

> 2026-09-12 Cloud relay client candidate: [selected-Navi HTTP transport](navi-cloud-relay-client.md) now preserves v2 turns, signs requests with short-lived AWS roles and performs fresh delivery checks. Gate 146 library tests and an actual Cloud bundle integration pass. Default ingress still uses one local upstream; central binding/selection, remote media and consumer activation remain implementation work. No release or OTA.

> 2026-09-12 Navi candidate: WhatsApp retains original event time and isolates the official account in routes and deduplication. Beacon handles bound members and stale conversation rejection. Next: route one official account to authenticated Navi endpoints without broadcasting household turns; preserve opaque continuation per endpoint and recheck binding on outgoing retries. Provider credentials remain only in Gate. Code/fixture tests precede Meta provisioning; rollout waits for all eight features.

## Guiding Baseline

HarborGate is the Rust IM gateway and northbound assistant/channel edge for
HarborBeacon.
The active IM service-to-service contract is
`HarborBeacon-HarborGate-Agent-Contract-v2.0.md`; the Android/Web/Gate edge
upgrade contract is `HarborBeacon-HarborGate-Agent-Contract-v3.0.md`.

## Current Milestone: Rust-Only Release Readiness

Exit criteria:

- Rust `harborgate` is the only packaged runtime.
- HarborBeacon release bundles include `harborgate/bin/harborgate` and no Python
  runtime fallback.
- `.82` live acceptance passes for Feishu and Weixin private messages.
- Harbor Assistant Messages shows connected/manage for configured IM connectors.
- Harbor Assistant Search remains same-origin through `/api/beacon/*`.
- Android/Web assistant chat turns enter through `POST /api/gateway/turns`.

## Next Milestones

1. Release hardening
   - keep musl builder lane green
   - keep setup/admin pages customer-facing
   - improve adapter error classification and observability

2. Feishu polish
   - expand interactive card delivery modes
   - keep native image delivery as the default image path
   - preserve webhook compatibility for controlled callback deployments

3. Weixin polish
   - stabilize private-DM long-poll observability
   - keep text/image/video/file native delivery paths covered
   - keep group chat outside ready scope until explicitly planned

4. Product-led prelaunch testing
   - use Harbor Assistant as the only WebUI validation entry
   - validate Search, Camera, Messages, and Settings as internal tabs
   - run end-to-end Beacon/Gate/WebUI release gates before tagging RC

## Permanent Boundary Rules

- HarborGate and HarborBeacon communicate only through HTTP/JSON.
- HarborGate keeps IM credentials and platform transport state.
- HarborGate owns channel-edge routing and proxying, but not Beacon-owned device
  or model configuration truth.
- HarborBeacon keeps business state, approvals, artifacts, audit, and model
  policy.
- HarborGate treats `conversation.handle`, `continuation`, and `active_frame` as
  opaque Beacon-owned values.
- HarborCloud entitlement, HarborLink MQTT command/ack, HarborDock remote
  home/camera control, and WebUI display state stay outside HarborGate.
# Navi WhatsApp delivery follow-up · 2026-09-12

Binding checks before sending and retrying are implemented through the internal Beacon endpoint documented in [the delivery profile](navi-whatsapp-delivery-authorization.md). Denial stops items; temporary check failures retry. Text, media preparation, cached uploads, notification origin and restart recovery are covered by Gate's 138 passing library tests. Continue multi-Navi authenticated routing and remote return transport; real Meta delivery and eight-function release acceptance remain open.

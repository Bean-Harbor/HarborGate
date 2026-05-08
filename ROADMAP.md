# HarborGate Rust Roadmap

## Guiding Baseline

HarborGate is the Rust IM gateway for HarborBeacon. The active cross-repo
contract is `HarborBeacon-HarborGate-Agent-Contract-v2.0.md`.

## Current Milestone: Rust-Only Release Readiness

Exit criteria:

- Rust `harborgate` is the only packaged runtime.
- HarborBeacon release bundles include `harborgate/bin/harborgate` and no Python
  runtime fallback.
- `.82` live acceptance passes for Feishu and Weixin private messages.
- Harbor Assistant Messages shows connected/manage for configured IM connectors.
- Harbor Assistant Search remains same-origin through `/api/harbor-assistant/*`.

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
- HarborBeacon keeps business state, approvals, artifacts, audit, and model
  policy.
- HarborGate treats `conversation.handle`, `continuation`, and `active_frame` as
  opaque Beacon-owned values.

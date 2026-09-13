# HarborGate Work Log

## 2026-09-12 · WhatsApp binding foundation

- Changed `adapters/whatsapp.rs` and `harborbeacon.rs`: preserve source timestamp,
  official-number route identity and account-scoped deduplication.
- Gate library: 132 tests passed; five WhatsApp tests include original timestamp,
  retry and official-number replacement checks. Existing v2 turn/result shapes
  remain unchanged; Beacon owns all member/home binding decisions.
- Companion Beacon/WebUI changes recover lost binding replies, isolate each
  binding generation and guard stale disconnect requests.
- Still pending: multi-Navi upstream routing, remote return/media paths, final
  outgoing binding checks and actual Meta validation. No deployment or OTA.

## 2026-05-01

### Rust-Only Runtime Cutover

- Promoted Rust `harborgate` to the only current HarborGate runtime.
- Archived the final Python-capable checkpoint with
  `archive/harborgate-python-runtime-final-20260501`.
- Retired Python runtime packaging from main; rollback now means installing an
  older verified release artifact.
- Verified live `.82` behavior before retirement: Feishu and Weixin private
  messages both reached HarborBeacon and received replies.
- Current release checks are Rust-first: `cargo fmt --check`, `cargo test`,
  `cargo build --release --bin harborgate`, and builder-side
  `cargo zigbuild --release --bin harborgate --target x86_64-unknown-linux-musl`.

## 2026-04-26

### v2.0 Control Pack Start

- Switched the active cross-repo baseline to `HarborBeacon-HarborGate-Agent-Contract-v2.0.md`.
- Marked v1.5 as historical reference only.
- Added the HarborGate v2.0 upgrade runbook and cutover checklist.
- Current focus is control-pack and drift-guard setup before business code migration.

### Stop-The-Line Rules

- Ask before adding public contract fields.
- Ask before changing Beacon/Gate ownership.
- Ask before adding v1.5 runtime compatibility.
- Ask before adding group-chat scope.
- Ask on live target, credential, DNS, or provider blockers.

## 2026-04-19

### Closeout Snapshot

- Feishu baseline rehearsal is ready on the frozen HarborBeacon `v1.5` seam.
- Weixin remains on the parity track with release-v1 ingress blockers only in the four fixed classes: `account_restore`, `qr_recovery`, `getupdates`, and `context_token_send`.
- The redacted gateway status can now export a more specific transport `blocker_category` such as `weixin_dns_resolution` while `release_v1.weixin_blocker_category` stays on the coarse parity bucket.
- `run_platform_live_gate.py` now keeps the latest real `ingress_probe` separate from `latest_successful_ingress_probe`, so a stale success report cannot hide the current blocker when Weixin is waiting for a new private text.
- The closeout stayed within HarborGate-only docs and verification; no HarborBeacon contract or recipient-shape changes were made.

### Validation Commands

```powershell
pytest tests/test_platform_live_gate.py tests/test_gateway.py tests/test_weixin_adapter.py
```

### Known Pending

- Weixin private-DM ingress still needs one of the four fixed blockers cleared before it reaches parity with the Feishu rehearsal surface.
- Group chats remain out of scope for this cutover and should not be used to widen the seam.

## 2026-04-18

### Contract Baseline Locked

- Froze [`HarborBeacon-HarborGate-Agent-Contract-v1.5.md`](./HarborBeacon-HarborGate-Agent-Contract-v1.5.md) as the working implementation guide for this repo.
- Aligned project governance so roadmap, execution plan, and work tracking all point to the same frozen contract.
- Confirmed the project should follow a Hermes-style separation:
  - one unified gateway and agent flow
  - one adapter per IM platform
  - adapters own platform protocol translation
  - HarborBeacon stays business-owner through the contract boundary

### Key Decisions

- `v1.5` is the current cross-repo freeze candidate and practical implementation baseline.
- Request-rejection failures and accepted-request delivery failures are now treated as separate channels.
- `VALIDATION_ERROR` is reserved for non-200 contract validation failures, not business `TaskResponse` failures.
- `route_key` is the preferred outbound routing handle.

### Next Execution Focus

1. Wire the inbound task path to the frozen `POST /api/tasks` contract.
2. Verify `needs_input` and `resume_token` behavior.
3. Implement the outbound notification delivery contract.
4. Use the GitHub repository as the shared management hub until a dedicated GitHub Projects board is enabled.

### Repository Tracking

- Created GitHub repository: `https://github.com/Bean-Harbor/harborbeacon-im-gateway`
- Local repo has been initialized and prepared for first push.
- Added `.gitignore` protections so local runtime state under `data/` is not committed.
- A dedicated GitHub Projects board is still pending because the current GitHub token does not include project scopes.

### Planning Update

- Expanded `PLAN.md` from a short milestone list into a full execution plan.
- Added phase goals, workstreams, acceptance criteria, risks, and a suggested two-week execution order.

### IM Contract Path Implementation

- Added an IM-side HarborBeacon task client for canonical `POST /api/tasks` requests.
- Taught the gateway to switch between local brain mode and HarborBeacon task-contract mode based on environment configuration.
- Added per-chat metadata persistence for `route_key`, `last_task_id`, `last_trace_id`, and `resume_token`.
- Extended adapters so inbound messages can carry `message_id`, `chat_type`, `route_key`, and protocol metadata into the contract layer.
- Added tests for contract request shape, local HTTP task posting, and resume-token reuse through the gateway.

### Notification Delivery Implementation

- Added IM-side `POST /api/notifications/deliveries`.
- Added route registration and route-key lookup inside the local gateway store.
- Added outbound idempotency caching and conflict detection for `delivery.idempotency_key`.
- Added shared error-envelope handling for notification request rejection failures.
- Added HTTP tests for success and `ROUTE_NOT_FOUND` behavior.

### Feishu And Weixin Runtime Upgrade

- Upgraded Feishu from protocol skeleton toward a real webhook-plus-send transport path.
- Added Feishu `url_verification` handling and callback token validation.
- Added Feishu live text send through tenant access token and message send API when explicitly enabled.
- Added persistent Weixin duplicate-update suppression so long-poll replay does not cause duplicate replies.
- Added tests for Feishu live-send mock flow, webhook verification, and Weixin duplicate tracking.

### Mobile Feishu Setup Portal

- Added a local setup portal that can be opened directly from a phone through `/setup`.
- Added `/setup/qr` and `/setup/qr.svg` so the desktop host can present a QR code for mobile onboarding.
- Ported the useful part of the old HarborBeacon flow: mobile form fields for `app_id`, `app_secret`, and optional `verification_token`, plus server-side credential validation before saving.
- Added runtime hot-apply for Feishu settings, so a running gateway can start using newly entered credentials without a manual edit on the host.
- Added a local setup state file for session code and Feishu credential persistence on the HarborGate machine.
- Added tests for setup status, QR SVG generation, and end-to-end Feishu configure flow through the HTTP server.

### Feishu Long-Connection Pivot

- Switched Feishu receive mode to websocket / long connection by default, matching the recommended Hermes and official Feishu approach for private or local deployments.
- Added adapter lifecycle management to the unified `GatewayService`, so live IM transports can start and stop inside the same gateway process instead of relying on sidecar scripts.
- Implemented a Feishu websocket runtime built on the official `lark-oapi` SDK, with inbound event forwarding back into the frozen HarborBeacon contract path.
- Kept webhook mode as an explicit fallback, but stopped using it as the default onboarding flow.
- Updated the mobile setup portal to guide users through long-connection setup instead of showing a misleading internal webhook URL.
- Added tests for Feishu websocket transport startup and event forwarding, and kept the optional webhook verification path covered.

### Live Feishu Validation

- Reconfigured the local runtime from the previously saved webhook state into websocket mode and restarted the service successfully.
- Confirmed the Feishu long-connection transport established a live connection to the official Feishu WebSocket endpoint.
- Verified that a real user DM entered the gateway, created a Feishu session file, and was normalized into the internal message flow.
- Verified outbound delivery back to the same Feishu route through the IM-side notification delivery endpoint, including a successful provider message ID from the Feishu Open Platform API.
- Closed the day with Feishu in `connected` websocket state, live send enabled, and the mobile setup portal aligned with the long-connection-first workflow.

### 2026-04-26 v2.0 Turn Client Closeout

- Preserved the v2.0 control pack as a separate commit.
- Switched the active HarborBeacon client default to `X-Contract-Version: 2.0`
  and `POST /api/web/turns`.
- Replaced request-time `resume_token` metadata with opaque `continuation`
  storage.
- Added coverage that Gate preserves an opaque continuation when Beacon returns
  a `completed` turn with an active frame, then clears it when Beacon omits the
  frame and continuation.
- Kept Weixin native video/file delivery in Gate, now driven by v2
  `delivery_hints`.
- Deployed the fresh Beacon frame-first bundle to `.182`; the direct
  `/api/web/turns` matrix now proves clip-confirmation feedback and boundary turns
  preserve continuation until playback or cancel.
- Renamed local release observability from `release_v1` to `release_v2`.
- Changed files: `src/im_agent/harborbeacon.py`, `src/im_agent/gateway.py`,
  `src/im_agent/setup_portal.py`, `tools/run_platform_live_gate.py`, and
  related tests.
- Tests run: `python -m pytest tests/test_v20_control_pack.py -q`,
  `python -m pytest tests/test_gateway.py::GatewayServiceTests::test_completed_active_frame_persists_and_reuses_continuation -q`,
  `python -m pytest tests/test_v20_control_pack.py tests/test_harborbeacon.py tests/test_gateway.py tests/test_weixin_adapter.py tests/test_platform_live_gate.py tests/test_server.py -q`,
  `python -m pytest`, and `git diff --check`.
- Drift check: Gate v2.0 guard passed; active client no longer posts
  `/api/tasks` or emits `args.resume_token`.
- Blockers: `.182` live Weixin validation is still pending target-registry
  confirmation.
- Next exact step: run the Weixin private-DM v2.0 matrix through the updated
  Gate client.
# 2026-09-12 Navi WhatsApp delivery checks

- Reproduced four outbound failures before the fix: revoked text sent, revocation during preparation ignored, uploaded media resent after restart/unbind, and unavailable authorization allowing send.
- Added the Beacon authorization call before materialization and each provider operation; no cached allow decision. Notifications persist their original conversation handle. Changed identity with the same idempotency key returns 409 rather than an accepted/retryable provider failure.
- Gate 138 library tests pass, including six delivery tests. Existing camera-download and notification-cache tests now exercise the new permission/profile behavior.
- Beacon remains the authority for binding/member state; v2 envelopes and other channels' fingerprint formats remain unchanged. See [profile](navi-whatsapp-delivery-authorization.md).
- No Meta call, `.154` operation, merge, image or OTA. Continue authenticated multi-Navi routing and remote media/text return.

## 2026-09-12 · Navi Cloud relay client

Implemented AWS-signed Cloud exchanges in the Beacon client, fixed request/deadline retries, fresh authorization, and refusal of unresolved remote artifacts. Added HTTP/credential tests and an actual Cloud bundle interoperability runner. See [client scope and validation](navi-cloud-relay-client.md). Fleet route selection and remote return transport remain unfinished; the eight-feature release condition is unchanged.

## 2026-09-12 · Navi endpoint selection

Added the durable Gate route directory, phone-proof relay and Owner-confirmation polling, device identity pinning before Cloud publication, scoped history and original-selection checks on queued delivery. Generic WhatsApp message aliases reject unsigned ingress. See [profile and remaining gates](navi-whatsapp-binding-proof.md). This is a development increment; no mainline merge, full image or OTA.

## 2026-09-12 · Central route receipt

Persist selection updates atomically, retry after transport failures/restart and fence late acknowledgements. New bindingRoute exchanges return activation/pause/retirement to the originating Beacon and browser. Gate tests, strict Clippy, actual Lambda asset interoperability and RISC-V compilation pass; [service profile](navi-whatsapp-binding-proof.md) records the remaining product gates. No mainline merge, full image or OTA.

# HarborGate Work Log

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
  and `POST /api/turns`.
- Replaced request-time `resume_token` metadata with opaque `continuation`
  storage.
- Kept Weixin native video/file delivery in Gate, now driven by v2
  `delivery_hints`.
- Renamed local release observability from `release_v1` to `release_v2`.
- Changed files: `src/im_agent/harborbeacon.py`, `src/im_agent/gateway.py`,
  `src/im_agent/setup_portal.py`, `tools/run_platform_live_gate.py`, and
  related tests.
- Tests run: `python -m pytest tests/test_v20_control_pack.py -q`,
  `python -m pytest tests/test_v20_control_pack.py tests/test_harborbeacon.py tests/test_gateway.py tests/test_weixin_adapter.py tests/test_platform_live_gate.py tests/test_server.py -q`,
  `python -m pytest`, and `git diff --check`.
- Drift check: Gate v2.0 guard passed; active client no longer posts
  `/api/tasks` or emits `args.resume_token`.
- Blockers: `.182` live Weixin validation is still pending target-registry
  confirmation.
- Next exact step: confirm `.182`, then run the Weixin private-DM v2.0 matrix
  through the updated Gate client.

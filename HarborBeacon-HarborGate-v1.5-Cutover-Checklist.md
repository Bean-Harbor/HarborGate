# HarborBeacon HarborGate v1.5 Cutover Checklist

## Historical Status

This document is historical after the 2026-04-26 decision to move the active
HarborBeacon <-> HarborGate seam to Contract v2.0.

Use `HarborBeacon-HarborGate-v2.0-Cutover-Checklist.md` for current work.

This repo is the IM-side evidence bundle for the frozen HarborBeacon seam in
`HarborBeacon-HarborGate-Agent-Contract-v1.5.md`.

## What is implemented here

- `POST /api/tasks` request shaping from IM inbound events
- stable `task_id`, `trace_id`, `source.route_key`, and `message.message_id`
- resume continuation by carrying `args.resume_token`
- `POST /api/notifications/deliveries` on the HarborGate side
- shared non-200 error envelope for request-rejection failures
- outbound delivery idempotency by `delivery.idempotency_key`
- route lookup and expiry handling for `destination.route_key`
- redacted optional `GET /api/gateway/status`

## Current Prelaunch Scope

- Feishu is the current baseline rehearsal surface and is ready on the frozen seam.
- Weixin `1:1` text is the parity-track surface until the same rehearsal matrix passes cleanly, and the only fixed blocker classes are `account_restore`, `qr_recovery`, `getupdates`, and `context_token_send`.
- Group chats stay out of scope; do not widen HarborBeacon semantics to support them.
- Both surfaces must keep the same HarborBeacon `v1.5` request and notification shape.

Current reproducible live-gate collector:

- `python .\tools\run_platform_live_gate.py`
- optional HarborBeacon-backed rehearsal:
  `python .\tools\run_platform_live_gate.py --task-api-url http://127.0.0.1:4175 --task-api-token <shared-token>`

Decision rule from the generated JSON report:

- `dual_surface_ready`: Feishu baseline and Weixin parity track both passed the rehearsal matrix
- `feishu_baseline_with_weixin_parity_track`: keep Feishu as the stable baseline while Weixin continues parity work
- `blocked`: stop cutover rehearsal until at least one live surface is healthy

## Known Pending

- `account_restore`: the saved Weixin account is missing or cannot be restored.
- `qr_recovery`: QR recovery or login refresh is still required before the account is usable.
- `getupdates`: the long-poll `getupdates` path is not stable or is timing out.
- `context_token_send`: inbound recovery works, but the follow-up context-token-backed send still fails.
- Anything outside those four classes should be treated as a separate regression, not a new Weixin cutover category.

## Weixin 1:1 Parity Pack

Use this path to decide whether Weixin has reached the same rehearsal level as Feishu.

1. Restore or create one Weixin account and confirm the gateway can recover it:
   - `WEIXIN_ACCOUNT_ID` points at a saved account
   - QR login can produce a valid account if restore is missing
   - `harborgate-weixin-runner` can long-poll `getupdates`
2. Confirm one private DM can enter the task-client path and emit:
   - `task_id`
   - `trace_id`
   - `route_key`
   - `message_id`
   - `status`
3. Run a HarborOS `service.restart` turn and confirm `needs_input -> resume` reuses:
   - the same `route_key`
   - the previous `resume_token`
   - a stable `message_task_ids` pointer for replay
4. Send one HarborBeacon notification delivery twice and confirm:
   - the cached `context_token` is reused
   - `delivery.idempotency_key` prevents a second platform send
   - `provider_message_id` stays stable on replay
5. Replay the original DM and confirm session pointers do not rewind.
6. Confirm one group message still fails fast as out of scope instead of widening the contract.

## Feishu Baseline Smoke Pack

Use this path as the stable baseline while Weixin is still working through parity gaps.

1. Start HarborGate with Feishu enabled and `IM_AGENT_CONTRACT_VERSION=1.5`.
2. Open `GET /api/gateway/status` and confirm:
   - the `feishu` channel is present
   - `display_name` is redacted and does not fall back to raw `app_id`
   - `transport.status`, `transport.mode`, and `transport.last_error` are visible
   - `transport.last_error` does not expose secrets
3. Send one inbound Feishu event through the active transport.
4. Confirm the inbound log line contains:
   - `task_id`
   - `trace_id`
   - `route_key`
   - `message_id`
   - `status`
5. Trigger a resumed turn, then send the same notification delivery twice.
6. Confirm the delivery log line contains:
   - `notification_id`
   - `delivery.idempotency_key`
   - `provider_message_id` when the platform returns one
   - `retryable` and `status`
7. Replay the original inbound message and confirm session pointers do not rewind.
8. Confirm the same HarborBeacon `service.status`, `service.restart`, and `files.list` scenarios can rerun without changing contract shape.

## Retrieval & Attachment Ingress Pack

Use this pack for natural-language retrieval turns that may carry images or files. Keep the attachment payload opaque and do not interpret it in HarborGate.

1. Send a webhook-style inbound with a retrieval-like text query plus one or more attachments.
2. Confirm the request shaping preserves:
   - `message.message_id`
   - `message.attachments` as opaque transport dictionaries
   - `source.route_key` and `source.session_id`
   - `args.resume_token` when present
3. Confirm the gateway log line includes:
   - `raw_text`
   - `content_kind=retrieval_candidate` when the text or attachments look retrieval-oriented
   - `attachment_count`
   - `attachment_types`
   - `attachment_metadata_keys`
4. Confirm the log does not print attachment values such as file keys, download URLs, or document names.
5. Replay the same inbound event and verify the replay path still preserves route and session continuity.

## Local Retrieval Round-Trip Launch Pack

Use this when you want a repeatable IM-side evidence path for retrieval traffic. It is safe to run locally because it only checks shape, counts, and rendered text.

1. Run the targeted seam smoke:
   - `python -m unittest discover -s tests -p "test_gateway.py"`
2. In the rich-reply case, confirm the output contains:
   - a rendered `检索结果` header
   - a short `引用` section
   - a short `附件` section
   - `retrieval_reply_rendered` in the gateway logs
3. In the rollback/degrade case, confirm:
   - the IM reply is still readable as a normal chat reply
   - `retrieval_reply_rendered` does not appear
   - no citation or artifact values are invented by IM
4. Collect the following evidence if you are sharing the run with HarborBeacon:
   - `content_kind`
   - `retrieval_render_kind`
   - `citation_count`
   - `artifact_count`
   - `route_key`
   - `session_id`
   - the final rendered reply text

## Attachment Ingress Rules

- Keep attachment objects opaque from the HarborGate point of view.
- Preserve transport metadata fields such as file keys, mime type, size, names, URLs, and provider-specific IDs when they arrive from the adapter.
- Do not rewrite attachment metadata into business terms like document title, scene, or knowledge source.
- Do not drop attachments just because the current business flow is text-first.
- Do not leak raw attachment values into logs or redacted status output.

## What HarborBeacon can rely on

- `source.route_key` is opaque and IM-owned
- repeated inbound retries reuse identity for the same IM event
- a resumed turn uses `args.resume_token`
- accepted delivery attempts return HTTP 200, even when the platform send later fails
- request-rejection failures use the shared HTTP error envelope
- `/api/gateway/status` returns redacted channel state and does not expose raw platform credentials

## What remains optional or non-frozen

- `/api/gateway/status` is supporting-only and not part of the two frozen interfaces
- setup portal routes such as `/api/setup/status` and `/api/setup/feishu/configure`
- Feishu webhook mode
- long-connection runtime details
- any platform-specific credential bootstrap flow

## Current IM-side verification

- contract-version checks are enforced on service-to-service endpoints
- service auth is enforced when `IM_AGENT_SERVICE_TOKEN` is configured
- the redacted status endpoint is covered by targeted tests and includes safe transport diagnostics
- the notification delivery endpoint still distinguishes request rejection from accepted-request delivery failure
- gateway logs include the main canary observability fields where available
- retrieval-style ingress logs include opaque attachment summaries without leaking attachment values
- gateway outbound metadata now includes a safe adapter profile so non-Feishu surfaces can be added without changing HarborBeacon semantics
- Weixin `1:1` gateway tests cover private-DM ingress, `needs_input -> resume`, notification replay idempotency, and replay-stable session pointers
- Feishu remains the rollback-safe adapter surface on the same frozen seam

## Cutover gate

- HarborBeacon may treat this repo as ready for seam validation only after the
  targeted tests pass and the cross-repo live round-trip still succeeds.
- The adapter-specific smoke pack should be runnable without revealing raw platform credentials in status output.
- If the external platform adapter changes, keep the frozen request and
  response shapes stable and update the checklist with the new evidence.
- If Weixin requires group-chat semantics to keep moving, stop and roll back to Feishu instead of widening the seam.

## Canary Note

Watch these signals during retrieval traffic:

- `content_kind` should flip to `retrieval_candidate` when the user sends a retrieval-like query or any attachment arrives.
- `route_key`, `session_id`, and `message_id` should stay stable across a replay of the same inbound event.
- `retrieval_reply_classified` should appear on every task-client retrieval turn and tell you whether the reply rendered as `retrieval_reply` or `plain_reply`.
- Retrieval replies should log `retrieval_reply_rendered` with only counts and section names, not raw citation or artifact values.
- If HarborBeacon rolls back the NL retrieval fallback or returns only a plain reply body, IM should degrade to a normal chat reply and `retrieval_reply_rendered` should not appear.
- The local round-trip launch pack should produce one rich reply evidence point and one plain-degrade evidence point from the same retrieval-style query shape.
- `attachment_count` and `attachment_types` should describe the transport shape only; they should not invent semantic labels.
- `attachment_metadata_keys` may expand as transports add fields, but raw values such as file names, URLs, and provider secrets must stay out of logs.
- If `/api/gateway/status` is used while diagnosing a canary, confirm `transport.last_error` is redacted and no credential-like values are present.

## Surface Expansion Note

- The gateway is now profile-driven at the seam level, so new chat surfaces should plug in through adapter normalization and `get_profile()` rather than by adding Feishu-only branches.
- WeChat / Weixin remains a reasonable candidate for the next surface if the transport path is ready, but this repo should treat it as an adapter addition, not a HarborBeacon semantics change.

# HarborGate v2.0 Master Plan

## Baseline

This plan is pinned to
[`HarborBeacon-HarborGate-Agent-Contract-v2.0.md`](./HarborBeacon-HarborGate-Agent-Contract-v2.0.md).

v2.0 is the active cross-repo baseline. v1.5 is historical reference only.
There is no in-process v1.5/v2.0 dual stack.

## Mission

Build the HarborGate side of the v2.0 seam while keeping the boundary clean:

- HarborGate owns IM adapters, ingress, route management, credentials, and
  outbound delivery.
- HarborBeacon owns conversation turns, active frames, business execution,
  approvals, artifacts, and audit.

## Success Criteria

- HarborGate receives a real IM message and sends a canonical `POST /api/web/turns`
  request to HarborBeacon.
- HarborBeacon returns a v2 turn response with user-renderable `reply.text`.
- HarborGate stores `conversation.handle` and `continuation` opaquely.
- HarborGate does not route business behavior from `active_frame.kind`.
- HarborGate delivers artifacts using platform-neutral `delivery_hints`.
- HarborBeacon remains free of direct IM delivery and raw platform credentials.

## Delivery Phases

### Phase 0: Control Pack

Status: in progress

- Publish the v2.0 contract.
- Update README, ROADMAP, PLAN, WORKLOG, runbook, and checklist.
- Add drift guard tests for remaining v1.5 active paths.

### Phase 1: Turn Client

Status: planned

- Replace the historical task-client path with a v2 turn client.
- Use `X-Contract-Version: 2.0`.
- Send inbound IM turns to `/api/web/turns`.
- Replace `message_task_ids` with `message_turn_ids`.

### Phase 2: Continuation Cache

Status: planned

- Replace `resume_token` metadata with opaque `continuation`.
- Cache only `conversation.handle`, `frame_id`, continuation `token`,
  `reply_to_turn_id`, and `expires_at`.
- Do not interpret active-frame business semantics.

### Phase 3: Delivery Hints

Status: planned

- Keep notification delivery hosted by HarborGate.
- Use v2 `delivery_hints` for native media selection.
- Keep Weixin native video/file fallback as Gate-side delivery behavior.

### Phase 4: Live Evidence

Status: planned

- Run local tests.
- Run platform live gate.
- Capture the Weixin private-DM matrix from the v2 checklist.

## Drift Guards

The project is not release-ready while any of these remain in active code:

- `X-Contract-Version: 1.5`
- HarborGate posts to `/api/tasks`
- request builders emit `args.resume_token`
- Gate routes on Beacon `active_frame.kind`
- group chat is treated as ready path

## Stop-The-Line Conditions

Stop and ask before continuing when:

- a new public v2 contract field is needed
- Beacon/Gate ownership would change
- v1.5 runtime compatibility is requested
- group chat is needed
- live target, credential, DNS, or provider state blocks the path

## Verification

- `python -m pytest`
- targeted: `python -m pytest tests/test_gateway.py tests/test_harborbeacon.py tests/test_weixin_adapter.py tests/test_platform_live_gate.py`
- live: `python .\tools\run_platform_live_gate.py --task-api-url http://127.0.0.1:4174 --task-api-token <shared-token>` after target confirmation

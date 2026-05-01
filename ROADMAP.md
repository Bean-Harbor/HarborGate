# HarborGate v2.0 Roadmap

## Guiding Baseline

The active implementation guide is
[`HarborBeacon-HarborGate-Agent-Contract-v2.0.md`](./HarborBeacon-HarborGate-Agent-Contract-v2.0.md).

v1.5 is historical reference only. Current work must follow the v2.0 upgrade
runbook and cutover checklist.

## Project Goal

Build the HarborGate transport side of the v2.0 conversation-turn seam:

- HarborGate owns platform adapters, ingress, outbound delivery, route registry,
  and platform credentials.
- HarborBeacon owns conversation handles, active frames, business execution,
  approvals, artifacts, and audit.

## Phases

### Phase 0: v2.0 Control Pack

Status: in progress

- Publish the v2.0 contract.
- Update management docs.
- Add drift guards that expose v1.5 active paths.

Exit criteria:

- README, PLAN, ROADMAP, WORKLOG, runbook, and checklist point to v2.0.
- Guard tests exist for contract version, `/api/tasks`, `args.resume_token`, and
  active-frame semantic routing.

### Phase 1: Inbound Turn Path

Status: planned

- Make HarborGate send canonical `POST /api/web/turns` requests.
- Ensure stable inbound idempotency with `turn_id`, `trace_id`,
  `transport.message_id`, and `transport.route_key`.
- Map HarborBeacon v2 turn responses back into user-visible IM replies without
  changing business meaning.

Exit criteria:

- Real IM inbound round-trip passes through
  `HarborGate -> /api/web/turns -> v2 turn response -> user reply`.
- Same-message retry with the same `turn_id` is proven idempotent.
- Conflicting replay of the same `turn_id` is rejected.

### Phase 2: Continuation Flow

Status: planned

- Store and replay Beacon-owned `conversation.handle`.
- Store and replay opaque `continuation`.
- Keep workflow truth in HarborBeacon.

Exit criteria:

- Real active-frame continuation passes end to end.
- Gate does not parse business active-frame semantics.

### Phase 3: Outbound Delivery

Status: planned

- Keep `POST /api/notifications/deliveries` hosted by HarborGate.
- Use v2 `delivery_hints`.
- Keep native video/file fallback inside platform adapters.

Exit criteria:

- Real `HarborBeacon -> HarborGate -> platform delivery` notification succeeds.
- Conflicting replay of the same `delivery.idempotency_key` is rejected.

### Phase 4: Weixin Evidence

Status: planned

- Stabilize Weixin private-DM text flow on the v2 seam.
- Validate conversation, clarification, cancel, record, and playback cases.
- Keep group chat out of scope.

Exit criteria:

- Weixin private-DM matrix in the v2 checklist passes or records an external
  blocker.

## Working Principles

- Contract-first before adapter-first.
- Platform logic stays in adapters.
- Business logic stays in HarborBeacon.
- No shared runtime state across repos.
- No v1.5/v2.0 in-process compatibility.
- Release only after the v2.0 release gate is satisfied.

# HarborGate Roadmap

## Guiding Baseline

- The frozen implementation guide for this project is [`HarborBeacon-HarborGate-Agent-Contract-v1.5.md`](./HarborBeacon-HarborGate-Agent-Contract-v1.5.md).
- `v1.5` is the working cross-repo contract baseline unless both repos explicitly approve a newer version.
- Roadmap decisions, delivery sequencing, and acceptance gates should be interpreted through the `v1.5` contract.

## Project Goal

Build an independent HarborGate that replaces the IM-facing layer in HarborBeacon while preserving a clean boundary:

- HarborGate owns platform adapters, ingress, outbound delivery, and route management.
- HarborBeacon owns business execution, resumable workflow state, approvals, artifacts, and audit.

## Phases

### Phase 0: Contract Freeze and Governance

Status: done

- Freeze `v1.5` as the implementation baseline.
- Keep earlier `v1.x` files for historical context only.
- Align roadmap, plan, and work log with the frozen contract.

### Phase 1: Inbound Task Path

Status: next

- Make HarborGate send canonical `POST /api/tasks` requests.
- Ensure stable inbound idempotency with `task_id`, `trace_id`, `message.message_id`, and `source.route_key`.
- Map HarborBeacon `TaskResponse` back into user-visible IM replies without changing business meaning.

Exit criteria:

- Real IM inbound round-trip passes through `HarborGate -> /api/tasks -> TaskResponse -> user reply`.
- Same-message retry with the same `task_id` is proven idempotent.
- Conflicting replay of the same `task_id` is rejected.

### Phase 2: Resume and Needs-Input Flow

Status: planned

- Support HarborBeacon `status=needs_input`.
- Persist and replay `resume_token` using a new `task_id` for the follow-up user message.
- Keep resumed turns on the HarborBeacon-owned business flow, not in HarborGate session state.

Exit criteria:

- Real `needs_input -> resumed turn` flow passes end to end.

### Phase 3: Outbound Notification Delivery

Status: planned

- Implement `POST /api/notifications/deliveries` on HarborGate.
- Prefer `destination.route_key` over platform-native addressing.
- Enforce outbound idempotency and delivery failure channel separation from `v1.5`.

Exit criteria:

- Real `HarborBeacon -> HarborGate -> platform delivery` notification succeeds.
- Conflicting replay of the same `delivery.idempotency_key` is rejected.

### Phase 4: Adapter Maturity

Status: planned

- Stabilize Weixin private text flow.
- Upgrade Feishu from protocol skeleton to real transport.
- Add more adapters only after the core contract path is stable.

Priority order:

1. Weixin hardening
2. Feishu websocket/text transport
3. Additional IM platforms

### Phase 5: HarborBeacon Cutover

Status: planned

- Remove HarborBeacon direct IM delivery.
- Remove HarborBeacon long-term ownership of IM platform credentials.
- Use HarborGate as the only IM-facing runtime.

Exit criteria:

- HarborBeacon no longer depends on direct platform credential validation for live IM delivery.
- HarborGate is the single owner of platform delivery and routing.

## Working Principles

- Contract-first before adapter-first.
- Platform logic stays in adapters.
- Business logic stays in HarborBeacon.
- No shared runtime state across repos.
- Release only after the `v1.5` release gate is satisfied.

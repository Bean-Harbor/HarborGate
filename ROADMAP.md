# IM Gateway Roadmap

## Guiding Baseline

- The frozen implementation guide for this project is [`HarborNAS-IM-Gateway-Agent-Contract-v1.5.md`](./HarborNAS-IM-Gateway-Agent-Contract-v1.5.md).
- `v1.5` is the working cross-repo contract baseline unless both repos explicitly approve a newer version.
- Roadmap decisions, delivery sequencing, and acceptance gates should be interpreted through the `v1.5` contract.

## Project Goal

Build an independent IM Gateway that replaces the IM-facing layer in HarborNAS while preserving a clean boundary:

- IM Gateway owns platform adapters, ingress, outbound delivery, and route management.
- HarborNAS owns business execution, resumable workflow state, approvals, artifacts, and audit.

## Phases

### Phase 0: Contract Freeze and Governance

Status: done

- Freeze `v1.5` as the implementation baseline.
- Keep earlier `v1.x` files for historical context only.
- Align roadmap, plan, and work log with the frozen contract.

### Phase 1: Inbound Task Path

Status: next

- Make IM Gateway send canonical `POST /api/tasks` requests.
- Ensure stable inbound idempotency with `task_id`, `trace_id`, `message.message_id`, and `source.route_key`.
- Map HarborNAS `TaskResponse` back into user-visible IM replies without changing business meaning.

Exit criteria:

- Real IM inbound round-trip passes through `IM Gateway -> /api/tasks -> TaskResponse -> user reply`.
- Same-message retry with the same `task_id` is proven idempotent.
- Conflicting replay of the same `task_id` is rejected.

### Phase 2: Resume and Needs-Input Flow

Status: planned

- Support HarborNAS `status=needs_input`.
- Persist and replay `resume_token` using a new `task_id` for the follow-up user message.
- Keep resumed turns on the HarborNAS-owned business flow, not in IM Gateway session state.

Exit criteria:

- Real `needs_input -> resumed turn` flow passes end to end.

### Phase 3: Outbound Notification Delivery

Status: planned

- Implement `POST /api/notifications/deliveries` on IM Gateway.
- Prefer `destination.route_key` over platform-native addressing.
- Enforce outbound idempotency and delivery failure channel separation from `v1.5`.

Exit criteria:

- Real `HarborNAS -> IM Gateway -> platform delivery` notification succeeds.
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

### Phase 5: HarborNAS Cutover

Status: planned

- Remove HarborNAS direct IM delivery.
- Remove HarborNAS long-term ownership of IM platform credentials.
- Use IM Gateway as the only IM-facing runtime.

Exit criteria:

- HarborNAS no longer depends on direct platform credential validation for live IM delivery.
- IM Gateway is the single owner of platform delivery and routing.

## Working Principles

- Contract-first before adapter-first.
- Platform logic stays in adapters.
- Business logic stays in HarborNAS.
- No shared runtime state across repos.
- Release only after the `v1.5` release gate is satisfied.

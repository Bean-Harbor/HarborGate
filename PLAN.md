# IM Gateway Plan

## Frozen Guide

- This plan is pinned to [`HarborNAS-IM-Gateway-Agent-Contract-v1.5.md`](./HarborNAS-IM-Gateway-Agent-Contract-v1.5.md).
- If implementation pressure conflicts with the contract, we change the plan first and the code second.

## Current Objective

Deliver the first production-shaped IM Gateway that can replace the HarborNAS IM layer through the frozen `v1.5` contract.

## Workstreams

### Stream A: IM Gateway Repo

Owner: IM Gateway engineer

- Keep the gateway loop platform-agnostic.
- Implement adapter-specific ingress and outbound delivery.
- Own route registry, delivery idempotency, and platform credential handling.
- Expose the frozen notification delivery endpoint.

Immediate tasks:

1. Lock the internal normalized models to the `v1.5` boundary expectations.
2. Implement HarborNAS task-client wiring for `POST /api/tasks`.
3. Implement route-key-aware outbound delivery plumbing.
4. Promote Feishu from skeleton to real transport after the contract path is stable.

### Stream B: HarborNAS Repo

Owner: HarborNAS engineer

- Keep HarborNAS as the business source of truth.
- Accept canonical inbound task payloads from IM Gateway.
- Preserve resumable workflow state and approval flow.
- Generate notification intent only, not platform-native sends.

Immediate tasks:

1. Align `assistant_task_api` behavior with inbound idempotency conflict rules.
2. Preserve and reuse `source.route_key` in business conversation state.
3. Emit notification requests that follow the `v1.5` delivery contract.
4. Remove direct platform-delivery assumptions from the IM path.

## Integration Milestones

### Milestone 1: Inbound Contract Path

- IM Gateway can submit a canonical task request.
- HarborNAS can answer with a user-renderable `TaskResponse`.
- The reply is delivered back to the originating IM conversation.

### Milestone 2: Resume Flow

- HarborNAS returns `needs_input`.
- IM Gateway re-enters the same business flow with `args.resume_token`.

### Milestone 3: Notification Contract Path

- HarborNAS sends notification intent to IM Gateway.
- IM Gateway resolves delivery via `destination.route_key`.

### Milestone 4: Cutover Readiness

- Contract tests pass in both repos.
- At least one real IM adapter has passed full round-trip validation.
- HarborNAS no longer needs direct IM delivery ownership.

## Coordination Rules

- Both engineers treat `v1.5` as normative, not illustrative.
- Cross-repo changes start with contract impact review, not ad hoc field changes.
- Any new field added during implementation must be justified against the frozen contract.

## Done Definition

- The release gate from `v1.5` is satisfied.
- The inbound path, resume path, and notification path each pass at least one real round-trip.
- Retry/idempotency conflict behavior is proven in tests.

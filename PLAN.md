# HarborGate Master Plan

## Baseline

- This plan is pinned to [`HarborBeacon-HarborGate-Agent-Contract-v1.5.md`](./HarborBeacon-HarborGate-Agent-Contract-v1.5.md).
- If implementation pressure conflicts with the contract, the team updates the plan first and code second.
- `v1.5` remains the working cross-repo baseline unless both repos explicitly approve a newer version.

## Mission

Build an independent HarborGate that replaces the HarborBeacon IM-facing layer while keeping a clean hard boundary:

- HarborGate owns IM platform adapters, ingress, route management, and outbound delivery.
- HarborBeacon owns business execution, resumable workflow state, approvals, artifacts, and audit.

## Success Criteria

The project is successful when all of the following are true:

- HarborGate can receive a real IM message and send a canonical `POST /api/tasks` request to HarborBeacon.
- HarborBeacon can return a user-renderable `TaskResponse` that HarborGate sends back without changing business meaning.
- HarborBeacon can return `status=needs_input` and HarborGate can continue the same business flow with `args.resume_token`.
- HarborBeacon can send notification intent to HarborGate through `POST /api/notifications/deliveries`.
- HarborGate becomes the only long-term owner of IM delivery and IM platform credentials.

## Scope Boundaries

### In Scope

- Inbound IM message normalization
- HarborBeacon task-client wiring
- Route-key-based outbound delivery
- Resume flow support
- Adapter maturation for Weixin and Feishu
- Contract tests, integration tests, and cutover support

### Out of Scope for This Delivery Wave

- A second cross-repo contract beyond `v1.5`
- Moving all intent parsing into HarborBeacon
- Multi-step async-only IM turns without an initial synchronous reply
- Broad multi-platform expansion before the first contract path is stable

## Working Model

### Principles

- Contract-first before adapter-first
- Platform logic stays in adapters
- Business logic stays in HarborBeacon
- No shared runtime state across repos
- Idempotency behavior is a first-class feature, not a later patch
- Real round-trips matter more than mock-only success

### Ownership Split

#### HarborGate Engineer

- gateway runtime
- normalized transport models
- adapter registry
- route registry
- platform delivery
- delivery idempotency
- IM platform credential handling
- HarborBeacon HTTP client integration

#### HarborBeacon Engineer

- `assistant_task_api`
- business state machine
- resumable workflow state
- approval flow
- artifact and audit persistence
- notification intent generation
- removal of direct platform-delivery assumptions

#### Shared Integration Ownership

- contract fixtures
- end-to-end acceptance tests
- error mapping decisions
- cutover checklist

## Delivery Phases

### Phase 0: Governance and Freeze

Status: done

Goals:

- Freeze `v1.5` as the implementation baseline.
- Align roadmap, plan, and work log with the frozen contract.
- Use the GitHub repository as the common tracking hub.

Artifacts:

- `HarborBeacon-HarborGate-Agent-Contract-v1.5.md`
- `ROADMAP.md`
- `PLAN.md`
- `WORKLOG.md`
- GitHub repo setup

### Phase 1: Internal Alignment in HarborGate

Status: next

Goals:

- Align internal gateway models and flow with the contract boundary.
- Prepare the codebase so HarborBeacon integration plugs into a stable, platform-agnostic path.

Tasks:

1. Confirm one internal inbound message shape and one outbound message shape.
2. Add a HarborBeacon task client abstraction for `POST /api/tasks`.
3. Define gateway-side mapping from internal inbound message to canonical HarborBeacon task request.
4. Define gateway-side mapping from `TaskResponse` to outbound delivery requests.
5. Make route key and message identity first-class in the gateway runtime.

Deliverables:

- internal request/response mapping layer
- HarborBeacon client configuration surface
- clear separation between adapter payloads and contract payloads

Acceptance:

- A local simulated inbound event can be transformed into a contract-valid HarborBeacon request.
- A simulated HarborBeacon response can be rendered back into adapter-facing outbound content.

### Phase 2: Inbound Task Contract Path

Status: planned

Goals:

- Make HarborGate send real canonical `POST /api/tasks` requests.
- Make HarborBeacon accept and process those requests in the expected shape.

Tasks:

1. Implement actual HTTP task submission from HarborGate to HarborBeacon.
2. Populate `task_id`, `trace_id`, `source`, `intent`, `message`, `entity_refs`, `args`, and `autonomy` correctly.
3. Ensure retry behavior reuses the same `task_id` for the same inbound event.
4. Preserve `source.route_key` for later replies and follow-up notifications.
5. Add contract fixtures for inbound success, retry, and conflict paths.

Dependencies:

- HarborGate task client
- HarborBeacon request validation and idempotency logic

Acceptance:

- Real IM inbound round-trip passes through `HarborGate -> /api/tasks -> TaskResponse -> user reply`.
- Same-message retry with the same `task_id` is idempotent.
- Conflicting replay of the same `task_id` is rejected with `409` and `IDEMPOTENCY_CONFLICT`.

### Phase 3: Resume and Needs-Input Flow

Status: planned

Goals:

- Support HarborBeacon resumable business flows from IM.

Tasks:

1. Support `status=needs_input`, `prompt`, and `resume_token`.
2. Persist the minimum gateway state needed to continue the same conversation cleanly.
3. Send the next user message as a new `task_id` with `args.resume_token`.
4. Verify HarborBeacon treats `resume_token` as business continuity, not idempotency identity.
5. Add contract fixtures for resumed turns.

Acceptance:

- Real `needs_input -> resumed turn` flow passes end to end.
- Retry of the same follow-up message stays idempotent.
- Resume flow does not require HarborGate to become the source of truth for workflow state.

### Phase 4: Outbound Notification Delivery Contract

Status: planned

Goals:

- Move HarborBeacon notification delivery behind HarborGate.

Tasks:

1. Implement `POST /api/notifications/deliveries` in HarborGate.
2. Resolve routes primarily through `destination.route_key`.
3. Enforce `delivery.mode` validation and outbound idempotency.
4. Separate request-rejection failures from accepted-request delivery failures.
5. Add contract fixtures for success, retry, conflict, and route expiry paths.

Dependencies:

- route registry persistence
- HarborBeacon notification producer

Acceptance:

- Real `HarborBeacon -> HarborGate -> platform delivery` round-trip succeeds.
- Same `delivery.idempotency_key` retry does not duplicate end-user delivery.
- Conflicting replay of the same `delivery.idempotency_key` is rejected.
- `ROUTE_NOT_FOUND` and `ROUTE_EXPIRED` return non-200 shared-envelope failures, not `200 ok=false`.

### Phase 5: Adapter Maturity

Status: planned

Goals:

- Make at least one live adapter fully trustworthy.
- Promote Feishu from skeleton to working transport after the core contract path is stable.

Priority order:

1. Weixin hardening
2. Feishu live transport
3. Additional IM platforms

#### Weixin Hardening

Tasks:

- harden long-poll runner behavior
- verify context token lifecycle
- verify outbound retry and error handling
- verify message identity stability

Acceptance:

- real private-text round-trip is stable under repeated use
- outbound failures are surfaced clearly

#### Feishu Live Transport

Tasks:

- add websocket-first transport
- support text receive/send through the real Feishu connection
- keep group gating and mention logic inside the adapter

Acceptance:

- real Feishu text round-trip works through the gateway
- adapter keeps Feishu protocol concerns out of the agent core

### Phase 6: Cutover and Hardening

Status: planned

Goals:

- Make HarborGate the only long-term IM delivery owner.

Tasks:

1. Remove HarborBeacon direct IM delivery from the live path.
2. Remove HarborBeacon long-term ownership of IM platform credentials.
3. Add redacted status support if HarborBeacon UI needs connection state.
4. Run cutover validation and rollback-readiness checks.

Acceptance:

- HarborBeacon no longer depends on direct platform credential validation for live IM delivery.
- HarborGate is the only runtime that talks to IM platforms for delivery.

## Workstreams

### Stream A: Contract Implementation in HarborGate

Priority:

1. HarborBeacon task client
2. request/response mapping
3. route-key-aware outbound delivery
4. notification delivery endpoint

### Stream B: HarborBeacon Contract Compliance

Priority:

1. inbound idempotency behavior
2. route key persistence
3. `needs_input` and resume flow compliance
4. notification producer behavior

### Stream C: Shared Validation

Priority:

1. JSON schema
2. golden fixtures
3. cross-repo contract tests
4. real adapter smoke tests

## Suggested Execution Order

### Immediate Sequence

1. Implement HarborGate HarborBeacon task client and config.
2. Wire canonical task request building from the internal inbound message model.
3. Build reply mapping from HarborBeacon `TaskResponse` into adapter-facing outbound content.
4. Align HarborBeacon with `task_id` replay and idempotency conflict behavior.
5. Validate the first real inbound round-trip.
6. Implement `needs_input` continuation path.
7. Implement notification delivery endpoint and route-key resolution.
8. Validate the first real outbound notification round-trip.
9. Harden Weixin.
10. Promote Feishu to live transport.

### Recommended Two-Week Focus

#### Week 1

- Day 1-2: HarborGate task client and request builder
- Day 2-3: HarborBeacon inbound contract alignment
- Day 3-4: first end-to-end inbound round-trip
- Day 4-5: retry and idempotency conflict validation

#### Week 2

- Day 1-2: `needs_input` and resumed turn flow
- Day 2-3: notification delivery endpoint
- Day 3-4: route-key and outbound idempotency validation
- Day 4-5: first cutover-ready demo path

## Deliverables Checklist

- frozen contract baseline documented
- gateway request builder
- HarborBeacon task client
- inbound contract fixtures
- resumed-turn support
- notification delivery endpoint
- outbound contract fixtures
- route registry behavior
- one stable live adapter
- cutover checklist

## Test Strategy

### Contract Tests

- request schema conformance
- response schema conformance
- idempotent inbound replay
- inbound replay conflict rejection
- resumed-turn behavior
- outbound delivery idempotency
- outbound delivery conflict rejection
- route expiry handling
- failure channel separation

### Integration Tests

- simulated HarborBeacon round-trip from HarborGate
- simulated notification round-trip into HarborGate
- adapter-to-gateway-to-HarborBeacon smoke flow

### Real Environment Validation

- one real inbound IM round-trip
- one real `needs_input` resumed turn
- one real outbound notification round-trip

## Risks and Mitigations

### Risk: Contract Drift During Implementation

Mitigation:

- any contract-impacting change must be discussed before code lands
- fields are not added ad hoc in only one repo

### Risk: HarborBeacon Still Leaks Platform Logic

Mitigation:

- keep `route_key` as the preferred outbound handle
- block new platform-native send shortcuts during review

### Risk: Retry Behavior Is Correct in Mocks but Wrong in Live Traffic

Mitigation:

- test same-message retries against the real adapter path
- log `task_id`, `trace_id`, `message.message_id`, and `route_key`

### Risk: HarborGate Starts Owning Business State by Accident

Mitigation:

- keep resume flow dependent on `resume_token`
- keep workflow truth in HarborBeacon only

### Risk: Feishu Expansion Distracts from the Core Contract Path

Mitigation:

- do not prioritize Feishu live transport before the core inbound and outbound contract path is working

## Done Definition

The project is done for this delivery wave when:

- the `v1.5` release gate is satisfied
- the inbound path, resume path, and notification path each pass at least one real round-trip
- retry and idempotency conflict behavior is proven in tests
- HarborBeacon no longer owns live IM delivery

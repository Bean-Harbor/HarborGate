# HarborBeacon IM Gateway Agent Contract v1.1 Proposal

## Status

Recommended disposition for `HarborBeacon-HarborGate-Agent-Contract-v1.md`:

- accept the overall architecture direction
- do not freeze as final v1 until the blocking items below are written as normative rules

This proposal is based on:

- the current contract draft `HarborBeacon-HarborGate-Agent-Contract-v1.md`
- the current HarborBeacon implementation in `src/bin/assistant_task_api.rs`
- the current HarborBeacon task runtime in `src/runtime/task_api.rs`
- the current direct notification delivery path in `src/connectors/notifications.rs`

## What Is Already Good

The current draft gets the most important architectural split right:

- IM Gateway owns all IM-platform concerns
- HarborBeacon owns all business/task concerns
- both repos communicate only through HTTP/JSON
- the existing `POST /api/tasks` path is reused instead of inventing a second overlapping task entrypoint
- direct HarborBeacon-to-platform delivery is treated as transitional and should be removed after rollout

These points should remain unchanged in v1.1.

## Blocking Changes Before Freeze

### 1. Inbound Idempotency Must Be Defined on `task_id`, Not Just `trace_id`

Current HarborBeacon behavior creates or reloads task state using `task_id`. `trace_id` is useful for observability, but it is not currently the primary dedup key for task execution.

If the same inbound IM message is retried with:

- stable `trace_id`
- new `task_id`

then HarborBeacon may create a second business task run.

### Required v1.1 rule

For the same inbound user message retry, IM Gateway must reuse all of:

- `task_id`
- `trace_id`
- `message.message_id` when the platform exposes one

### Required HarborBeacon rule

HarborBeacon must treat repeated `POST /api/tasks` calls with the same `task_id` as idempotent replays of the same task intent, not as a new business turn.

### Required wording

Add a normative clause:

> For retries of the same inbound IM event, the IM Gateway MUST reuse the same `task_id`. A new `task_id` means a new business task, even if `trace_id` is unchanged.

### Why this matters

Without this rule, duplicate delivery or timeout retries can create:

- duplicate workflow state transitions
- duplicate approvals
- duplicate artifacts
- duplicate notifications

## 2. Intent Ownership Must Be Explicit

The current draft says HarborBeacon owns business/task concerns, but the canonical request already includes:

- `intent.domain`
- `intent.action`

The current HarborBeacon implementation dispatches directly on `domain + action`. That means v1 cannot stay ambiguous about who produces these fields.

### Required v1.1 decision

Choose one of the following and write it explicitly.

#### Option A: Transitional v1.1

- IM Gateway provides `intent.domain` and `intent.action`
- HarborBeacon remains the owner of execution semantics, state, approvals, artifacts, and audit
- this is a temporary coupling caused by current `assistant_task_api` implementation
- moving business intent resolution fully into HarborBeacon is a v2 concern

#### Option B: Strict ownership model

- IM Gateway provides only `intent.raw_text`
- HarborBeacon resolves `domain` and `action`
- HarborBeacon must first implement a text-to-task entry mode before freeze

### Recommendation

For immediate delivery, use Option A and label it clearly as transitional.

### Required wording

Add a normative clause:

> In v1.1, the IM Gateway MUST populate `intent.domain` and `intent.action` for task requests because the current HarborBeacon `assistant_task_api` dispatch path requires them. This does not transfer ownership of business workflow semantics to the IM Gateway; it is a transitional request-shaping requirement for compatibility with the current backend.

## 3. Notification Destination Semantics Must Be Stronger

The current draft allows `destination.platform` and `destination.recipient` to be optional. That is directionally fine, but it does not yet define a stable routing primitive owned by IM Gateway.

Current HarborBeacon code still resolves recipients using platform-specific binding rules such as:

- open id
- chat id
- display name matching
- requester user fallback

That logic should not leak back into the final cross-repo boundary.

### Required v1.1 addition

Add a preferred opaque route field:

```json
"destination": {
  "kind": "conversation",
  "route_key": "gw_route_01JABC...",
  "id": "optional-legacy-value",
  "platform": "optional",
  "recipient": {}
}
```

### Required routing rule

- `destination.route_key` is the preferred identifier for outbound delivery
- `route_key` is owned and interpreted only by IM Gateway
- if `route_key` is present, IM Gateway must not require HarborBeacon to understand platform-native ids
- if `route_key` is absent, then fallback routing requires enough explicit fields for delivery

### Recommendation

Use this fallback priority:

1. `destination.route_key`
2. `{destination.platform, destination.id}`
3. explicit `destination.recipient`

### Required wording

Add a normative clause:

> `destination.route_key` is the preferred outbound routing identifier in v1.1. It is an opaque IM Gateway-owned key. HarborBeacon MUST treat it as write-only routing metadata and MUST NOT infer platform semantics from it.

## Recommended Additions for v1.1

These are not as blocking as the items above, but they are high-value and should be added in the same revision if possible.

### 4. Contract Versioning

Add one explicit version signal, either:

- HTTP header: `X-Contract-Version: 1.1`
- JSON field: `"contract_version": "1.1"`

Recommendation:

- use the HTTP header for runtime negotiation
- optionally mirror it into logs

### 5. Repo-to-Repo Authentication

Both cross-repo endpoints should define a concrete auth mode:

- `POST /api/tasks`
- `POST /api/notifications/deliveries`

Minimum acceptable v1.1 rule:

- loopback-only bind for local deployment
- shared bearer token or signed HMAC for cross-process calls

Add a normative clause:

> Neither interface may rely on "localhost is trusted" as the only security boundary once cross-process or cross-host deployment is supported.

### 6. Timeout and Retry Ownership

Define:

- request timeout for `IM Gateway -> HarborBeacon`
- request timeout for `HarborBeacon -> IM Gateway`
- which side is allowed to retry
- retry backoff strategy
- max retry count
- idempotency key retention TTL

Recommended minimum:

- `IM Gateway -> HarborBeacon`: 15s request timeout, retry only on transport failure or 5xx, never on explicit business failure
- `HarborBeacon -> IM Gateway`: 10s request timeout, retry only when `retryable=true`
- idempotency key retention TTL: at least 24h

### 7. Attachment Access Contract

The current draft adds `message.attachments[].download.url`, which is the right idea, but the access contract should be fully explicit.

Add:

- `method`
- `headers`
- `auth`
- `expires_at`
- `max_size_bytes`

Recommended shape:

```json
"download": {
  "mode": "gateway_proxy",
  "url": "http://127.0.0.1:8787/files/att_01JABC...",
  "method": "GET",
  "headers": {
    "Authorization": "Bearer ..."
  },
  "auth": {
    "type": "bearer"
  },
  "expires_at": "2026-04-18T14:10:00Z",
  "max_size_bytes": 20971520
}
```

Also add a rule:

> HarborBeacon MUST treat `download.url` as opaque and MUST NOT assume local filesystem access to the underlying media.

### 8. Error Model for `/api/tasks`

The current task response already has `status`, but cross-repo handling will be cleaner if task failures can expose a machine-usable error code.

Recommended addition:

```json
"error": {
  "code": "VALIDATION_ERROR|UNSUPPORTED_ACTION|APPROVAL_REQUIRED|TEMPORARY_UNAVAILABLE",
  "message": "human-readable summary"
}
```

Rule:

- `status=failed` should still be the primary business failure signal
- `error.code` should explain why, without requiring text parsing

### 9. Content Ownership Rule

Add a rule to prevent semantic drift across repos:

> HarborBeacon owns business meaning. IM Gateway may adapt formatting for platform constraints, but it MUST NOT reinterpret, summarize, or alter business semantics contained in `TaskResponse` or notification payloads.

This avoids hidden LLM-like rewriting inside the Gateway layer.

### 10. Long-Running Task Policy

The current flow behaves like a synchronous task-response exchange. v1.1 should state whether that is a hard requirement.

Recommended v1.1 rule:

- `POST /api/tasks` must return a user-renderable result synchronously for supported IM turns
- long-running background work may emit later notifications, but the initial response still needs a usable reply

If async-only tasks are planned later, define them in v2 instead of silently overloading v1.1.

### 11. Observability Requirements

Require both repos to log:

- `task_id`
- `trace_id`
- `message.message_id`
- `notification_id`
- `delivery.idempotency_key`
- `destination.route_key` when present
- `provider_message_id` on successful delivery

This is essential for real-world support and replay debugging.

### 12. JSON Schema and Golden Fixtures

The current draft already calls for contract tests. v1.1 should make this stricter:

- one JSON Schema per request/response type
- one shared fixture set checked by both repos
- CI must validate both schema conformance and replay/idempotency behavior

## Suggested v1.1 Cross-Repo Models

### Inbound Task Request

Keep the current shape, but add:

- `message` as a required block for IM Gateway callers
- optional `contract_version`

Recommended required fields for IM Gateway callers:

- `task_id`
- `trace_id`
- `source.channel`
- `source.surface`
- `source.conversation_id`
- `source.user_id`
- `intent.raw_text`
- `intent.domain`
- `intent.action`
- `message.message_id` when available
- `message.chat_type`

### Outbound Notification Delivery Request

Keep the current shape, but add:

- `destination.route_key`
- optional `contract_version`

Recommended required fields:

- `notification_id`
- `trace_id`
- `destination.kind`
- `content.body`
- `delivery.mode`
- `delivery.idempotency_key`

## Proposed Normative Text Snippets

These can be copied almost directly into the next contract revision.

### Inbound Retry Rule

> For retries of the same inbound IM event, the IM Gateway MUST reuse the same `task_id`, `trace_id`, and, when available, `message.message_id`. A new `task_id` represents a new business task.

### Destination Routing Rule

> `destination.route_key` is the preferred outbound routing identifier. It is opaque and IM Gateway-owned. HarborBeacon MUST NOT depend on platform-specific interpretation of `route_key`.

### Attachment Access Rule

> Attachment download metadata is an opaque transport contract. HarborBeacon MUST use the provided access metadata and MUST NOT assume direct filesystem access or platform-native file identifiers.

### Business Semantics Rule

> HarborBeacon owns business semantics. IM Gateway may format output for a target IM platform, but MUST NOT reinterpret, summarize, or rewrite business meaning.

## Recommended Test Additions

Add these to the existing minimum test set:

1. same inbound message retried with same `task_id` does not create a second `TaskRun`
2. same inbound message retried with a different `task_id` is treated as a new task
3. notification delivery using `destination.route_key` succeeds without HarborBeacon providing platform-native recipient fields
4. expired attachment download metadata is rejected with a machine-readable error
5. notification retry with the same `idempotency_key` returns the same effective delivery result and does not duplicate user-visible output

## Recommended Final Position

Approve the current contract direction with required v1.1 revisions.

The draft is already correct on the large architectural decision:

- separate repos
- hard HTTP/JSON boundary
- HarborBeacon owns business state
- IM Gateway owns platform runtime

But v1.1 should not be declared frozen until it explicitly defines:

- inbound idempotency around `task_id`
- who supplies `intent.domain/action`
- outbound destination routing semantics

If those three items are fixed, the contract is strong enough for two engineers to develop independently and merge safely.

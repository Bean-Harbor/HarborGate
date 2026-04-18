# HarborBeacon HarborGate Agent Contract v1.5

## Status

This document supersedes the earlier v1.x drafts as the recommended working
contract for first independent implementation across:

- the external HarborGate repo
- the HarborBeacon backend repo

Recommended disposition:

- accept this as the working freeze candidate for cross-repo development
- treat the normative rules below as implementation requirements, not examples
- keep earlier v1.x files as historical context only

## Purpose

This contract freezes the boundary between:

- HarborGate, which owns all IM-platform concerns
- HarborBeacon backend, which owns all business and task concerns

Both sides communicate only through HTTP/JSON contracts.

## Frozen Interfaces

v1.5 freezes exactly two cross-repo interfaces:

1. inbound task interface
   - HarborGate -> HarborBeacon
   - based on existing `POST /api/tasks`
2. outbound notification delivery interface
   - HarborBeacon -> HarborGate
   - hosted by HarborGate

## Hard Boundary Rules

- HarborGate MUST NOT import `harborbeacon.*`, `orchestrator.*`, or any HarborBeacon runtime code.
- HarborBeacon MUST NOT import HarborGate adapter, runtime, or platform client code.
- The repos MUST NOT share `.harborbeacon/*.json` or any other runtime state files.
- IM platform credentials such as `app_id`, `app_secret`, bot token, websocket ticket, webhook secret, and refresh tokens MUST live only in HarborGate.
- HarborBeacon remains the source of truth for:
  - business session state
  - resumable workflow state
  - approval state
  - artifacts
  - audit trail
- HarborGate may keep only lightweight transport and runtime state such as:
  - websocket connection state
  - long-poll cursor
  - route registry
  - context token
  - temporary delivery cache
- HarborBeacon MUST NOT directly deliver platform messages after cutover of the notification interface.
- HarborBeacon MUST NOT remain the long-term owner of platform credential validation. If HarborBeacon needs connection status in UI or setup flow, it should obtain a redacted status result from HarborGate instead of storing and validating raw platform credentials itself.

## Ownership Split

- HarborGate owns:
  - adapters
  - webhook, websocket, and long-poll connection mode
  - message normalization
  - ingress session handling
  - outbound delivery
  - platform payload formatting
  - platform credential storage
  - route key generation and lookup
- HarborBeacon owns:
  - `assistant_task_api`
  - task execution
  - business state
  - approvals
  - artifacts
  - audit
  - notification intent generation
  - business conversation continuity

## Cross-Cutting Normative Rules

### Contract Versioning

- Both frozen interfaces MUST carry `X-Contract-Version: 1.5`.
- Either side MAY mirror the contract version into logs or payload metadata for debugging.

### Authentication

- Both frozen interfaces MUST require explicit service-to-service authentication.
- Local single-host deployment MAY use loopback bind plus a shared bearer token.
- Cross-host deployment MUST use authenticated transport and MUST NOT rely on "localhost is trusted" as the only security boundary.

### Timestamp Format

- All timestamp fields exchanged across the frozen interfaces MUST use RFC 3339 UTC format.
- Examples:
  - `2026-04-18T14:10:00Z`
  - `2026-04-18T14:10:00.123Z`
- Unix epoch timestamps MAY still exist inside private runtime stores, but they MUST NOT be used in cross-repo contract payloads.

### Timeout and Retry Ownership

- `HarborGate -> HarborBeacon`
  - request timeout SHOULD default to 15 seconds
  - retry only on transport failure or 5xx
  - do not retry on explicit business failure returned by HarborBeacon
- `HarborBeacon -> HarborGate`
  - request timeout SHOULD default to 10 seconds
  - retry only when the delivery response declares `retryable=true`
- HarborGate idempotency key retention TTL SHOULD be at least 24 hours.

### Observability

Both repos MUST log, when available:

- `task_id`
- `trace_id`
- `source.route_key`
- `source.conversation_id`
- `message.message_id`
- `notification_id`
- `delivery.idempotency_key`
- `destination.route_key`
- `provider_message_id`

### Content Ownership

- HarborBeacon owns business semantics.
- HarborGate MAY adapt formatting for platform constraints, but it MUST NOT reinterpret, summarize, or rewrite business meaning contained in `TaskResponse` or notification payloads.

### Long-Running Task Policy

- `POST /api/tasks` MUST return a user-renderable result synchronously for supported IM turns.
- Long-running background work MAY emit later notifications, but the initial task response still needs a usable reply.
- Async-only turn semantics are out of scope for v1.5 and belong in a later version.

### Shared HTTP Error Envelope

For contract, auth, validation, idempotency, routing, or infrastructure failures
that are returned as non-200 HTTP responses, both frozen interfaces MUST use the
same error envelope shape:

```json
{
  "ok": false,
  "error": {
    "code": "SERVICE_AUTH_FAILED|CONTRACT_VERSION_MISMATCH|VALIDATION_ERROR|IDEMPOTENCY_CONFLICT|ROUTE_NOT_FOUND|ROUTE_EXPIRED|INFRASTRUCTURE_ERROR",
    "message": "human-readable summary"
  },
  "trace_id": "optional-trace-id"
}
```

Rules:

- 401 or 403 are reserved for service-to-service auth failures and MUST use `SERVICE_AUTH_FAILED`.
- 404 is reserved for `ROUTE_NOT_FOUND`.
- 409 is reserved for `IDEMPOTENCY_CONFLICT`.
- 410 is reserved for `ROUTE_EXPIRED`.
- 422 is reserved for structurally valid requests that fail contract validation and MUST use `VALIDATION_ERROR`.
- 5xx is reserved for infrastructure or internal errors and MUST use `INFRASTRUCTURE_ERROR`.
- Business failures for supported turns SHOULD still come back as HTTP 200 with a business response envelope.

### Failure Channel Separation Rule

The contract distinguishes two failure channels and they MUST NOT be mixed:

1. request-rejection failures
   - request was not accepted for business handling or delivery attempt
   - MUST use non-200 HTTP plus the shared error envelope
2. accepted-request delivery failures
   - request was accepted and delivery was attempted
   - MUST use HTTP 200 plus the delivery response envelope with `ok=false`

The following codes MUST appear only in the shared non-200 error envelope:

- `SERVICE_AUTH_FAILED`
- `CONTRACT_VERSION_MISMATCH`
- `VALIDATION_ERROR`
- `IDEMPOTENCY_CONFLICT`
- `ROUTE_NOT_FOUND`
- `ROUTE_EXPIRED`
- `INFRASTRUCTURE_ERROR`

The following codes MUST NOT appear in the shared non-200 error envelope and
are reserved for accepted-request delivery failure results:

- `RATE_LIMIT`
- `PROVIDER_AUTH_FAILED`
- `INVALID_RECIPIENT`
- `PLATFORM_UNAVAILABLE`
- `UNSUPPORTED_CONTENT`

## Interface 1: Inbound Task Interface

### Endpoint

`POST /api/tasks`

### Strategy

This interface intentionally reuses the current `TaskRequest` shape instead of
inventing a second overlapping turn endpoint.

Existing top-level fields remain:

- `task_id`
- `trace_id`
- `step_id`
- `source`
- `intent`
- `entity_refs`
- `args`
- `autonomy`

One explicit top-level `message` block is required for HarborGate callers so
IM-specific metadata does not leak into `args`.

### Canonical Request

```json
{
  "task_id": "task_01JABC...",
  "trace_id": "trace_01JABC...",
  "step_id": "step_01",
  "source": {
    "channel": "feishu",
    "surface": "harborgate",
    "conversation_id": "oc_xxx",
    "user_id": "ou_xxx",
    "session_id": "gw_sess_01JABC...",
    "route_key": "gw_route_01JABC..."
  },
  "intent": {
    "domain": "camera",
    "action": "scan",
    "raw_text": "scan cameras"
  },
  "entity_refs": {},
  "args": {},
  "autonomy": {
    "level": "supervised"
  },
  "message": {
    "message_id": "om_xxx",
    "chat_type": "p2p",
    "mentions": [
      {
        "id": "ou_bot_xxx",
        "name": "HarborBeacon Bot"
      }
    ],
    "attachments": []
  }
}
```

### `source` Block Rules

- `source.channel`, `source.surface`, `source.conversation_id`, `source.user_id`, and `source.route_key` are required for HarborGate callers.
- `source.route_key` is an opaque HarborGate-owned route handle.
- HarborBeacon MUST treat `source.route_key` as write-only routing metadata.
- HarborBeacon SHOULD persist `source.route_key` in its business conversation state so later notifications can target the same IM route without HarborBeacon storing platform-native recipient semantics.
- `source.session_id` is ingress runtime metadata only. It MUST NOT be treated as the source of truth for HarborBeacon business workflow state.

### `message` Block Contract

```json
{
  "message_id": "platform_message_id",
  "chat_type": "p2p",
  "mentions": [
    {
      "id": "platform_user_id",
      "name": "Display Name"
    }
  ],
  "attachments": [
    {
      "attachment_id": "att_01JABC...",
      "type": "image",
      "name": "front-door.jpg",
      "mime_type": "image/jpeg",
      "size_bytes": 183920,
      "download": {
        "mode": "gateway_proxy",
        "url": "http://127.0.0.1:8787/files/att_01JABC...",
        "method": "GET",
        "headers": {
          "Authorization": "Bearer attachment_token"
        },
        "auth": {
          "type": "bearer"
        },
        "expires_at": "2026-04-18T14:10:00Z",
        "max_size_bytes": 20971520
      },
      "metadata": {
        "platform_file_key": "file_xxx"
      }
    }
  ]
}
```

### Attachment Rules

- `message` is required for HarborGate callers in v1.5.
- `message.message_id` is required when the platform exposes one.
- If the platform truly does not expose a stable message id, HarborGate MUST still keep retry identity stable using the same `task_id` and `trace_id`.
- `message.chat_type` MUST be one of:
  - `p2p`
  - `group`
  - `channel`
  - `unknown`
- `message.attachments` may be empty.
- HarborBeacon MUST treat `download.url` and access metadata as opaque transport contract details.
- HarborBeacon MUST NOT assume local filesystem access or platform-native file identifiers.

### Intent Ownership Rule

v1.5 adopts the transitional compatibility model:

- HarborGate MUST populate `intent.domain` and `intent.action`
- HarborBeacon remains the owner of execution semantics, state, approvals, artifacts, and audit
- this does not transfer business semantics ownership to HarborGate
- moving business intent resolution fully into HarborBeacon is a future-version concern

### Inbound Idempotency Rule

- For retries of the same inbound IM event, HarborGate MUST reuse all of:
  - `task_id`
  - `trace_id`
  - `message.message_id`, when the platform exposes one
  - `source.route_key`
- A new `task_id` means a new business task, even if `trace_id` is unchanged.
- HarborGate SHOULD derive or persist `task_id` from stable inbound event identity, such as platform plus conversation plus message id.
- If no platform message id exists, HarborGate MUST generate the `task_id` once and persist the mapping so retries reuse the same value.
- HarborBeacon MUST treat repeated `POST /api/tasks` calls with the same `task_id` as idempotent replays of the same task intent, not as a new business turn.

### Inbound Idempotency Conflict Rule

If HarborBeacon receives a repeated `task_id`, it MUST compare the new request
against the original request identity. At minimum, HarborBeacon MUST compare:

- `source.route_key`
- `source.conversation_id`
- `message.message_id`, when present
- `intent.domain`
- `intent.action`
- `intent.raw_text`
- normalized `entity_refs`
- normalized `args`

If the repeated `task_id` is accompanied by a materially different request
identity, HarborBeacon MUST reject it with:

- HTTP 409
- error code `IDEMPOTENCY_CONFLICT`

HarborBeacon MUST NOT silently overwrite the previously accepted business task
identity for an existing `task_id`.

### Backward Compatibility

- Legacy non-IM callers may omit `message` and `source.route_key`.
- HarborBeacon may initially treat those fields as optional during rollout for legacy callers only.
- Once HarborGate is the primary IM caller, `message` and `source.route_key` should be treated as required for IM surfaces.

### Response Contract

This interface keeps the existing `TaskResponse` envelope and adds an optional
machine-readable `error` block.

```json
{
  "task_id": "task_01JABC...",
  "trace_id": "trace_01JABC...",
  "status": "completed",
  "executor_used": "camera_hub_service",
  "risk_level": "LOW",
  "result": {
    "message": "scan completed",
    "data": {},
    "artifacts": [],
    "events": [],
    "next_actions": [
      "analyze living room camera"
    ]
  },
  "audit_ref": "audit_01JABC...",
  "missing_fields": [],
  "prompt": null,
  "resume_token": null,
  "error": null
}
```

Failure shape:

```json
{
  "task_id": "task_01JABC...",
  "trace_id": "trace_01JABC...",
  "status": "failed",
  "result": {
    "message": "operation failed",
    "data": {},
    "artifacts": [],
    "events": [],
    "next_actions": []
  },
  "error": {
    "code": "BUSINESS_RULE_VIOLATION|UNSUPPORTED_ACTION|TEMPORARY_UNAVAILABLE|ATTACHMENT_UNAVAILABLE|INTERNAL_ERROR",
    "message": "human-readable summary"
  }
}
```

### Response Rules

- For supported turns, HarborBeacon SHOULD return HTTP 200 with a business `TaskResponse`, even when `status=failed`.
- 4xx and 5xx are reserved for contract, auth, idempotency, or infrastructure failures.
- `status=needs_input` with `prompt` and `resume_token` continues the same HarborBeacon-owned business flow.
- `VALIDATION_ERROR` is reserved for non-200 shared contract-validation failures and MUST NOT appear in the business `TaskResponse.error.code` field.

### Resume Continuation Rule

When HarborBeacon returns `status=needs_input` with `resume_token`, HarborGate MUST include that token in the next `POST /api/tasks` request as `args.resume_token`.

The follow-up user message that satisfies the prompt is a new inbound IM event, not a retry of the previous inbound delivery. Therefore:

- the follow-up request MUST use a new `task_id`, unless it is retrying the same follow-up user message delivery
- retries of that same follow-up user message MUST reuse the same `task_id`, `trace_id`, and `message.message_id` when available
- HarborGate SHOULD preserve the same `source.conversation_id` and `source.route_key` when the follow-up happens in the same conversation
- HarborBeacon MUST use `resume_token` as the business-flow continuation handle, not as an idempotency key

Recommended resumed-turn example:

```json
{
  "task_id": "task_01NEW...",
  "trace_id": "trace_01NEW...",
  "source": {
    "channel": "feishu",
    "surface": "harborgate",
    "conversation_id": "oc_xxx",
    "user_id": "ou_xxx",
    "session_id": "gw_sess_01JABC...",
    "route_key": "gw_route_01JABC..."
  },
  "intent": {
    "domain": "camera",
    "action": "connect",
    "raw_text": "password xxxxxx"
  },
  "entity_refs": {},
  "args": {
    "resume_token": "resume_01JABC...",
    "password": "xxxxxx"
  },
  "autonomy": {
    "level": "supervised"
  },
  "message": {
    "message_id": "om_followup_xxx",
    "chat_type": "p2p",
    "mentions": [],
    "attachments": []
  }
}
```

### Gateway Reply Mapping

HarborGate should map `TaskResponse` to user-visible replies as follows:

- `result.message`
  - primary reply body
- `result.artifacts`
  - attachment or link rendering source
- `result.next_actions`
  - optional suggestion chips or appended text
- `status=needs_input` with `prompt` and `resume_token`
  - continue the same HarborBeacon-owned business flow
- `status=failed`
  - render failure message without reinterpreting HarborBeacon business semantics

## Interface 2: Outbound Notification Delivery Interface

### Endpoint

`POST /api/notifications/deliveries`

This endpoint is hosted by HarborGate.

### Canonical Request

```json
{
  "notification_id": "notif_01JABC...",
  "trace_id": "trace_01JABC...",
  "source": {
    "service": "harborbeacon",
    "module": "task_api",
    "event_type": "task.completed"
  },
  "destination": {
    "kind": "conversation",
    "route_key": "gw_route_01JABC...",
    "id": "optional-legacy-value",
    "platform": "optional",
    "recipient": {
      "recipient_id": "ou_xxx",
      "recipient_type": "open_id"
    }
  },
  "content": {
    "title": "Front Door AI Analysis",
    "body": "1 person detected.",
    "payload_format": "plain_text",
    "structured_payload": {},
    "attachments": []
  },
  "delivery": {
    "mode": "send",
    "reply_to_message_id": "",
    "update_message_id": "",
    "idempotency_key": "idem_01JABC..."
  },
  "metadata": {
    "correlation_id": "trace_01JABC..."
  }
}
```

### Destination Routing Rules

- `destination.route_key` is the preferred outbound routing identifier in v1.5.
- `route_key` is opaque and owned only by HarborGate.
- HarborBeacon MUST treat `route_key` as write-only routing metadata and MUST NOT infer platform semantics from it.
- If HarborBeacon is replying into the same IM conversation that produced an inbound task request, it SHOULD reuse the previously persisted `source.route_key` as `destination.route_key`.
- Routing fallback priority SHOULD be:
  1. `destination.route_key`
  2. `{destination.platform, destination.id}`
  3. explicit `destination.recipient`
- HarborBeacon SHOULD NOT depend on platform-native identifiers once `route_key` is available.

### Route Lifetime Rule

- HarborGate owns route key lifecycle and registry.
- If a `route_key` is no longer usable, HarborGate MUST reject the request using the shared non-200 error envelope:
  - HTTP 404 + `ROUTE_NOT_FOUND`, or
  - HTTP 410 + `ROUTE_EXPIRED`
- HarborBeacon MAY fall back to explicit legacy recipient fields only when they are present and only during migration.
- Route-state failures MUST NOT be returned as HTTP 200 delivery-failure payloads.

### Notification Rules

- HarborBeacon produces notification intent only.
- HarborGate performs actual platform delivery.
- HarborBeacon MUST NOT attach platform credentials to this request.
- `delivery.mode` MUST be one of:
  - `send`
  - `reply`
  - `update`

### Delivery Mode Field Rules

- If `delivery.mode=send`
  - `reply_to_message_id` MUST be empty
  - `update_message_id` MUST be empty
- If `delivery.mode=reply`
  - `reply_to_message_id` MUST be present
  - `update_message_id` MUST be empty
- If `delivery.mode=update`
  - `update_message_id` MUST be present
  - `reply_to_message_id` MUST be empty

Invalid field combinations MUST be rejected with:

- HTTP 422
- error code `VALIDATION_ERROR`

### Response Contract

This response shape is used only when the notification request was accepted and
delivery was attempted.

```json
{
  "delivery_id": "delivery_01JABC...",
  "notification_id": "notif_01JABC...",
  "trace_id": "trace_01JABC...",
  "ok": true,
  "status": "sent",
  "platform": "feishu",
  "provider_message_id": "om_xxx",
  "retryable": false,
  "error": null
}
```

Accepted-request delivery failure:

```json
{
  "delivery_id": "delivery_01JABC...",
  "notification_id": "notif_01JABC...",
  "trace_id": "trace_01JABC...",
  "ok": false,
  "status": "failed",
  "platform": "feishu",
  "provider_message_id": null,
  "retryable": true,
  "error": {
    "code": "RATE_LIMIT|PROVIDER_AUTH_FAILED|INVALID_RECIPIENT|PLATFORM_UNAVAILABLE|UNSUPPORTED_CONTENT",
    "message": "human-readable summary"
  }
}
```

Rules:

- accepted-request delivery failures MUST use HTTP 200
- `ROUTE_NOT_FOUND`, `ROUTE_EXPIRED`, `VALIDATION_ERROR`, `IDEMPOTENCY_CONFLICT`, and `SERVICE_AUTH_FAILED` MUST NOT appear in this response body
- `PROVIDER_AUTH_FAILED` means HarborGate was authenticated by HarborBeacon, but platform-provider credentials or platform-provider auth were invalid for the attempted delivery

### Delivery Idempotency Rule

- `delivery.idempotency_key` MUST stay stable for notification retries.
- HarborGate MUST avoid duplicate user-visible sends when the same idempotency key is retried.

### Delivery Idempotency Conflict Rule

If HarborGate receives the same `delivery.idempotency_key` again:

- and the effective delivery request is materially identical, HarborGate SHOULD return the same effective delivery result and SHOULD reuse the original `provider_message_id` when available
- and the effective delivery request is materially different, HarborGate MUST reject it with:
  - HTTP 409
  - error code `IDEMPOTENCY_CONFLICT`

At minimum, HarborGate MUST compare:

- `notification_id`
- `trace_id`
- `destination.route_key`
- `destination.id`
- `destination.platform`
- `destination.recipient`
- `content`
- `delivery.mode`
- `reply_to_message_id`
- `update_message_id`

If the same `delivery.idempotency_key` is reused with a different `notification_id`, HarborGate MUST reject it with `409` and `IDEMPOTENCY_CONFLICT`, even if the rendered content is identical.

HarborGate MUST NOT silently accept conflicting payloads for the same
`delivery.idempotency_key`.

## Auxiliary Status Interface (Non-Frozen)

This section documents a recommended supporting interface. It is not one of the
two frozen core interfaces above.

### Recommended Endpoint

`GET /api/gateway/status`

### Purpose

This endpoint allows HarborBeacon UI or setup flows to read redacted HarborGate
status without requiring HarborBeacon to store raw platform credentials.

### Authentication Rule

If this endpoint is implemented:

- it MUST NOT be unauthenticated
- it SHOULD require the same service-to-service authentication model used by the frozen interfaces when called by HarborBeacon backend services
- if exposed to local admin UI directly, it MUST require equivalent local admin authorization

### Recommended Response Shape

```json
{
  "ok": true,
  "gateway_version": "0.1.0",
  "channels": [
    {
      "platform": "feishu",
      "enabled": true,
      "connected": true,
      "display_name": "HarborBeacon Bot",
      "capabilities": {
        "reply": true,
        "update": true,
        "attachments": true
      }
    }
  ]
}
```

Rules:

- the response MUST be redacted
- raw platform credentials MUST NOT be returned
- HarborBeacon MAY consume this endpoint for UI status only
- this endpoint MUST NOT be treated as a replacement for either frozen business interface

## Business Session Truth

HarborBeacon persists business conversation state in `.harborbeacon/task-api-conversations.json`.

That means:

- resumable workflow truth stays in HarborBeacon
- HarborGate may keep only transport session helpers
- HarborGate MUST NOT become the source of truth for workflow or business state

## Credential and Setup Boundary

- HarborBeacon direct platform delivery is transitional and must be removed after rollout of the notification interface.
- HarborBeacon direct platform credential validation is also transitional and should be removed from long-term architecture.
- If HarborBeacon needs to show "connected" or "credential verified" in UI, the preferred long-term model is:
  - HarborGate stores and validates platform credentials
  - HarborGate exposes a redacted status result or admin API
  - HarborBeacon consumes only connection status, app display name, or route capability metadata
- HarborBeacon SHOULD NOT remain the owner of raw `app_id`, `app_secret`, bot token, or equivalent platform credentials after full cutover.

## Recommended Private Models

These are private implementation details, not shared cross-repo contracts.

- HarborGate private models:
  - internal `InboundMessage`
  - internal `OutboundMessage`
  - adapter runtime state
  - route registry records
- HarborBeacon private models:
  - `TaskRequest`
  - `TaskResponse`
  - `NotificationRequest`
  - task session state
  - approval, artifact, and audit models

## Recommended Implementation Split

- Engineer A: HarborGate repo
  - adapters
  - gateway runtime
  - route registry
  - session ingress
  - platform delivery
  - platform credential and config management
  - message normalization and reply formatting
- Engineer B: HarborBeacon repo
  - `assistant_task_api`
  - business and task state machine
  - approval flow
  - artifact and audit persistence
  - notification intent generation
  - replacing direct IM send with HarborGate delivery calls

## Minimum Test Cases

1. HarborGate -> `POST /api/tasks` happy path with `source.route_key`, `message.message_id`, `chat_type`, `mentions`, and `attachments`.
2. HarborBeacon task resume path returns `status=needs_input` with `prompt` and `resume_token`.
3. A follow-up user turn that satisfies the prompt sends a new `task_id` plus `args.resume_token` and successfully resumes the HarborBeacon flow.
4. Same inbound message retried with the same `task_id` does not create a second business task transition.
5. Same inbound message retried with a different `task_id` is treated as a new task.
6. Same `task_id` replayed with conflicting task identity is rejected with `409` and `IDEMPOTENCY_CONFLICT`.
7. HarborBeacon -> HarborGate notification send using `destination.route_key` succeeds without HarborBeacon providing platform-native recipient fields.
8. Notification retry with the same `idempotency_key` does not duplicate end-user delivery.
9. Same `idempotency_key` replayed with conflicting notification payload is rejected with `409` and `IDEMPOTENCY_CONFLICT`.
10. Reusing the same `idempotency_key` with a different `notification_id` is rejected with `409` and `IDEMPOTENCY_CONFLICT`.
11. Expired or invalid attachment download metadata is rejected with a machine-readable error.
12. Expired or missing `route_key` returns HTTP 404 or 410 using the shared HTTP error envelope, not a 200 delivery failure response.
13. Provider-credential failure during an accepted delivery attempt returns HTTP 200 with `ok=false` and `error.code=PROVIDER_AUTH_FAILED`.
14. Invalid `delivery.mode` field combinations are rejected with `422` and `VALIDATION_ERROR`.
15. Business task failure MUST NOT use `VALIDATION_ERROR` in the business response envelope.
16. HarborBeacon build or contract test fails if direct IM platform credential usage remains in the notification delivery path after full cutover.

## JSON Schema and Fixtures

- There MUST be one JSON Schema per request and response type for both frozen interfaces.
- Both repos MUST validate against the same golden fixture set.
- CI MUST cover:
  - schema conformance
  - inbound replay and idempotency behavior
  - resumed turn behavior with `args.resume_token`
  - outbound notification idempotency behavior
  - route key happy path and expiry path
  - delivery mode field validation
  - delivery failure channel separation
  - HTTP error envelope conformance

## Rollout Order

1. First, make HarborGate call `POST /api/tasks` and map `TaskResponse` back to user replies.
2. In the same phase, ensure HarborGate always provides stable `task_id`, `trace_id`, and `source.route_key` for retries of the same inbound event.
3. In the same phase, ensure resumed turns send `args.resume_token` while using a new `task_id` for the new user message.
4. Then, extract HarborBeacon notification delivery behind the new HTTP notification interface.
5. Then, persist and reuse `route_key` from HarborBeacon business conversation state for follow-up notifications.
6. Then, add the non-frozen redacted gateway status endpoint if HarborBeacon UI needs connection state.
7. Finally, remove HarborBeacon direct IM platform delivery and direct platform credential validation so HarborGate fully owns the IM layer.

## Release Gate

A release is allowed only when:

- both frozen interfaces have contract tests
- one real IM round-trip passes through `HarborGate -> /api/tasks -> TaskResponse -> user reply`
- one real `needs_input -> resumed turn` round-trip passes through `HarborGate -> /api/tasks -> HarborBeacon resume flow -> user reply`
- one real notification round-trip passes through `HarborBeacon -> HarborGate -> platform delivery`
- same-message retry with the same `task_id` is proven idempotent
- conflicting replay of the same `task_id` is proven rejected
- conflicting replay of the same `delivery.idempotency_key` is proven rejected
- accepted-request provider delivery failures are proven distinct from non-200 request-rejection failures
- HarborBeacon no longer depends on platform credentials for IM notification delivery
- the intended migration plan away from HarborBeacon-owned platform credential validation is agreed and tracked

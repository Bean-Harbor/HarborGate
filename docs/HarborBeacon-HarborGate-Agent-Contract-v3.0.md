# HarborBeacon HarborGate Agent Contract v3.0

## Status

This is the HarborGate northbound edge upgrade contract.

It extends the v2.0 turn / conversation / continuation seam without renaming
HarborGate and without moving HarborBeacon business ownership into HarborGate.
The active IM service-to-service seam may continue to use v2.0 during the
migration window; v3.0 defines the public northbound gateway shape for Android,
Web chat, and future channel clients.

Authoritative v2 reference:

- `HarborBeacon-HarborGate-Agent-Contract-v2.0.md`

## Purpose

v3.0 generalizes HarborGate from an IM-only transport edge into a northbound
channel edge. HarborGate can accept turns from IM, Android, Web chat, and future
client surfaces, then forward normalized turn envelopes to HarborBeacon.

HarborBeacon remains the business core and source of truth for:

- conversation truth and continuation
- task and workflow state
- approvals, artifacts, and audit
- knowledge, model, device, and camera configuration
- planner, router, and domain execution semantics

HarborGate owns only channel-edge concerns:

- IM adapters and platform credentials
- Android/Web channel binding, route lifecycle, and delivery metadata
- route keys, push/channel delivery handles, and redacted gateway status
- northbound proxying to Beacon-owned APIs

## Hard Boundary Rules

- HarborGate must not own Home Device, HarborOS, knowledge, model, approval, or
  audit truth.
- HarborGate must not interpret `active_frame.kind` or device configuration
  semantics.
- HarborBeacon must not own channel delivery, push tokens, route lifecycle, or
  IM platform credentials.
- Device credentials and camera configuration may pass through HarborGate as an
  HTTP proxy request, but they must be persisted and audited by HarborBeacon.
- HarborGate and HarborBeacon still communicate only through HTTP/JSON.
- The repos must not import each other's runtime code or share runtime state
  files.

## Interface 1: Gateway Turn

### Endpoint

```text
POST /api/gateway/turns
```

This is the client-facing northbound turn endpoint hosted by HarborGate.
HarborGate normalizes the request and forwards it to HarborBeacon's turn seam.

### Request Shape

```json
{
  "turn": {
    "turn_id": "turn_android_01",
    "trace_id": "trace_android_01",
    "occurred_at": "2026-05-09T10:00:00Z",
    "retry_of": null
  },
  "actor": {
    "user_id": "user_1",
    "workspace_id": "home-1",
    "account_id": "acct_1"
  },
  "conversation": {
    "handle": "conv_01JABC",
    "channel": "android",
    "surface": "android",
    "thread_id": "device_install_1",
    "chat_type": "p2p"
  },
  "transport": {
    "route_key": null,
    "message_id": "client_msg_1",
    "capabilities": {
      "text": true,
      "image": true,
      "file": true,
      "video": true
    },
    "metadata": {
      "client_version": "1.0.0"
    }
  },
  "input": {
    "text": "帮我看看客厅摄像头",
    "parts": []
  },
  "continuation": null,
  "autonomy": {
    "level": "supervised"
  }
}
```

### Rules

- `conversation.channel` may be `weixin`, `feishu`, `android`, `webui`, or a
  future channel key.
- `conversation.handle` remains Beacon-owned and opaque to HarborGate.
- `transport.route_key` remains HarborGate-owned and opaque to HarborBeacon.
- If the client omits `transport.route_key`, HarborGate derives and stores one.
- HarborGate may cache only opaque `conversation.handle` and `continuation`
  values returned by HarborBeacon.
- HarborGate must not persist or forward raw Android push tokens as Beacon
  business metadata.

## Interface 2: Beacon Admin/Core Proxy

### Endpoint

```text
/api/beacon/*
/api/harbor-gate/api/beacon/*
```

These are equivalent client-facing proxy prefixes for Beacon-owned
admin/config/core APIs. HarborGate forwards the request to HarborBeacon by
stripping the public prefix and using the corresponding Beacon internal
`/api/*` path. `/api/beacon/*` remains supported for existing clients;
`/api/harbor-gate/api/beacon/*` supports the HarborGate service-entry layout.

Examples:

```text
/api/beacon/knowledge/search -> HarborBeacon /api/knowledge/search
/api/harbor-gate/api/beacon/knowledge/search -> HarborBeacon /api/knowledge/search
/api/beacon/devices/manual -> HarborBeacon /api/devices/manual
/api/beacon/models/endpoints -> HarborBeacon /api/models/endpoints
/api/beacon/cameras/camera-1/snapshot.jpg -> HarborBeacon /api/cameras/camera-1/snapshot.jpg
```

### Rules

- HarborGate MUST authenticate the following user JSON requests before
  forwarding them:
  - `POST /knowledge/search`
  - `GET /knowledge/conversations`
  - `GET /knowledge/conversations/{conversation_id}`
  - `DELETE /knowledge/conversations/{conversation_id}`
  - `PATCH /knowledge/conversation-settings`
- The rule applies identically through `/api/beacon/*`,
  `/api/harbor-gate/api/beacon/*`, and the deprecated
  `/api/harbor-assistant/*` alias.
- The client supplies a fresh `X-HarborOS-Auth-Token` for each protected
  request. The WebUI MUST create it with a 30-second TTL, empty attributes,
  `match_origin=false`, and `single_use=true`. `match_origin=false` is required
  because Gate validates the browser-issued token over its loopback middleware
  connection.
- HarborGate MUST validate the token by calling `auth.login_ex` with
  `TOKEN_PLAIN` over `ws://127.0.0.1:6000/api/current`, with a five-second
  end-to-end timeout and reconnect-token issuance disabled.
- A successful principal MUST have `user_info.privilege.webui_access=true` and
  the `FULL_ADMIN` role. The canonical principal ID is
  `harboros:uid:<pw_uid>`.
- HarborGate MUST discard the one-time token after validation and MUST NOT
  forward, persist, or log it.
- HarborGate MUST remove all client-provided identity headers, identity query
  parameters, and `Authorization`. It MUST replace them on protected requests
  with:
  - `X-Harbor-Principal-Source: harboros`
  - `X-Harbor-Principal-Id: harboros:uid:<pw_uid>`
  - `X-Harbor-Principal-Roles: <validated middleware roles>`
  - `X-Harbor-Workspace-Id: <configured workspace, default home-1>`
- HarborGate MUST use its configured `HARBORBEACON_WEB_API_TOKEN` as the
  upstream bearer. It MUST NOT use the legacy IM
  `HARBORBEACON_TASK_API_TOKEN` as a proxy fallback. It MUST fail closed when
  the Web API service token is unavailable and MUST NOT forward a browser
  bearer as a fallback.
- Missing, expired, or invalid one-time tokens return `401`. Missing WebUI
  access or `FULL_ADMIN` returns `403`. Middleware connection, protocol, or
  timeout failures and unavailable Gate-to-Beacon service credentials return
  `503`.
- HarborGate must not parse or persist Beacon-owned request bodies such as
  device credentials, model endpoint secrets, knowledge roots, or camera
  settings.
- Media proxy requests MUST preserve `Range` and `If-Range`. HarborGate MUST
  preserve upstream `206`, `Accept-Ranges`, `Content-Range`, `Content-Length`,
  `ETag`, and `Last-Modified` semantics so preview seeking remains functional.
- `/api/harbor-assistant/*` is a deprecated migration alias only. New UI,
  Android, or docs work must use `/api/beacon/*`.

## Release Gate

v3.0 northbound readiness requires:

- `POST /api/gateway/turns` can forward Android/Web turns into Beacon and return
  the Beacon turn response.
- Continuation across Android/Web turns keeps the same Beacon conversation
  handle.
- HarborGate stores continuation opaquely and does not route on business frame
  kinds.
- `/api/beacon/*` proxies to Beacon internal `/api/*` paths without storing
  device credentials or model secrets.
- The protected knowledge JSON endpoints authenticate a fresh HarborOS
  single-use token, replace all client identity, and forward only the Gate
  service bearer plus the canonical principal headers.
- Missing/invalid token, insufficient privilege, unavailable middleware, and
  missing service-token regressions return the frozen `401`/`403`/`503`
  classes without exposing token material.
- The v2.0 IM turn and notification seams remain unchanged.
- v2.0 IM private-message regression still passes.

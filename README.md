# HarborGate

This repository is the HarborGate northbound transport gateway for HarborBeacon.

It is inspired by the architecture of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent), especially these ideas:

- keep the agent core separate from messaging platform adapters
- normalize inbound events into one internal message model
- store sessions per chat instead of coupling memory to a single frontend
- make the LLM backend swappable

This project does not copy Hermes source code. It borrows the architecture direction and re-implements a much smaller starter in our own structure.

## Project governance

The project is now pinned to [`HarborBeacon-HarborGate-Agent-Contract-v2.0.md`](./HarborBeacon-HarborGate-Agent-Contract-v2.0.md) as the active cross-repo implementation guide.

`HarborBeacon-HarborGate-Agent-Contract-v1.5.md` remains historical reference
only.

Management documents:

- [`ROADMAP.md`](./ROADMAP.md)
- [`PLAN.md`](./PLAN.md)
- [`WORKLOG.md`](./WORKLOG.md)
- [`HarborBeacon-HarborGate-v2.0-Upgrade-Runbook.md`](./HarborBeacon-HarborGate-v2.0-Upgrade-Runbook.md)
- [`HarborBeacon-HarborGate-v2.0-Cutover-Checklist.md`](./HarborBeacon-HarborGate-v2.0-Cutover-Checklist.md)

## What is included

- a `GatewayService` that routes inbound platform events
- a `PlatformAdapter` abstraction for IM adapters
- a small adapter registry so platforms are plugged into one gateway flow
- a generic `WebhookAdapter` that we can use immediately
- a first-pass `WeixinAdapter` for personal WeChat text messages
- a `FeishuAdapter` with websocket-first receive mode, real text send, native image send, and card-action normalization
- a file-based session store
- a default rule-based brain for local testing
- an optional OpenAI-compatible backend through environment variables
- a Python fallback runtime for rollback
- a Rust HarborGate runtime (`harborgate`) for setup/admin pages, Feishu, Weixin, webhook, delivery, and runtime supervision

## Hermes-style platform coverage

HarborGate now keeps Feishu and Weixin as the current live paths, while exposing a broader Hermes-style adapter skeleton for the rest of the IM surface.

Current live adapters:

- `feishu`
- `weixin`
- `webhook`

Current placeholder adapters:

- `telegram`
- `discord`
- `slack`
- `whatsapp`
- `signal`
- `email`
- `wecom`

What the placeholder layer means today:

- each platform is registered through the same adapter registry and gateway server flow
- `POST /messages/<platform>` can already normalize a canonical inbound payload
- `/api/gateway/status` reports each platform as `not_configured`, `configured_placeholder`, or `live`
- placeholder outbound delivery returns a stable simulated delivery payload instead of raising random transport errors

This keeps the HarborBeacon v2.0 seam controlled while giving us a near-usable skeleton for future live transports.

## Layout

```text
src/im_agent/
  brain.py            # rule-based or OpenAI-compatible response backend
  gateway.py          # orchestrates adapters, sessions, and replies
  models.py           # normalized message dataclasses
  server.py           # HTTP entrypoint
  session_store.py    # file-based chat history
  platforms/
    base.py           # platform adapter interface
    registry.py       # adapter registration and enable/disable logic
    webhook.py        # generic inbound JSON adapter
    weixin.py         # personal WeChat (iLink) adapter
    feishu.py         # Feishu / Lark adapter with long-connection runtime
```

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m im_agent.server
```

The server starts on `127.0.0.1:8787` by default.

## Rust runtime

Rust is the default HarborGate runtime. Python remains packaged as an explicit
rollback fallback:

```powershell
cargo test
cargo build --release --bin harborgate
$env:HARBORGATE_RUNTIME='rust'
.\target\release\harborgate.exe
```

The release bundle can also use the `harborgate` binary alias because HarborBeacon
copies the Rust gateway into `harborgate/bin/harborgate`.

Important runtime defaults:

- `HARBORGATE_RUNTIME=rust` is the release default.
- `HARBORGATE_RUNTIME=python` is reserved for rollback.
- `HARBORGATE_RUNTIME=auto` may still select Rust when the binary is present.
- `HARBORGATE_RUST_FEISHU_WEBSOCKET=0` can disable Feishu websocket receive when needed.

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
python -m im_agent.server
```

Health check:

```bash
curl http://127.0.0.1:8787/health
```

Send a demo message:

```bash
curl -X POST http://127.0.0.1:8787/messages/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "platform": "feishu",
    "chat_id": "demo-room",
    "user_id": "u-1001",
    "message_id": "om_demo_001",
    "text": "你好，帮我确认链路是否正常"
  }'
```

Example response:

```json
{
  "platform": "feishu",
  "chat_id": "demo-room",
  "text": "[feishu] I received: 你好，帮我确认链路是否正常\nThe clean-room gateway is working. Set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL to switch from demo replies to a real model.",
  "timestamp": "2026-04-17T12:00:00+00:00",
  "metadata": {
    "adapter": "webhook"
  }
}
```

## Optional OpenAI-compatible backend

If these environment variables are set, the gateway will call a real model instead of the demo rule engine:

```bash
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_API_KEY="your-api-key"
export LLM_MODEL="gpt-4.1-mini"
```

Supported contract:

- `POST {LLM_BASE_URL}/chat/completions`
- OpenAI-compatible JSON response shape

## HarborBeacon web turn API mode

If `HARBORBEACON_WEB_API_URL` or `HARBORBEACON_TASK_API_URL` is set, HarborGate posts v2.0 turn envelopes to HarborBeacon. The default endpoint is `/api/web/turns`; `/api/turns` is accepted only as a deprecated compatibility alias during the Beacon single-port cutover.

```powershell
$env:HARBORBEACON_WEB_API_URL='http://127.0.0.1:4174'
$env:HARBORBEACON_TASK_API_TOKEN='replace-me'
$env:HARBORBEACON_CONTRACT_VERSION='2.0'
$env:HARBORBEACON_DEFAULT_DOMAIN='general'
$env:HARBORBEACON_DEFAULT_ACTION='message'
$env:HARBORBEACON_AUTONOMY_LEVEL='supervised'
```

If both HarborBeacon URLs are unset, the gateway falls back to the local rule-based brain or the OpenAI-compatible backend.

## Current prelaunch scope

This repo currently treats the cross-repo prelaunch rehearsal like this:

- Feishu v1.5 evidence is historical baseline only while the v2.0 turn seam is built
- Weixin `1:1` private DM is the active live surface for v2.0 proof
- the redacted gateway summary may export transport `blocker_category` such as `weixin_dns_resolution`, but contract readiness is judged by the v2.0 runbook
- only when the v2.0 private-DM matrix passes do we call the result ready
- HarborBeacon v2.0 active ingress uses `POST /api/web/turns` while keeping notification delivery in HarborGate
- Weixin group chats remain explicitly out of scope for this round

Recommended live-gate collector:

```powershell
python .\tools\run_platform_live_gate.py
```

Optional HarborBeacon-backed rehearsal, when a task API endpoint is already running:

```powershell
python .\tools\run_platform_live_gate.py `
  --task-api-url http://127.0.0.1:4174 `
  --task-api-token your-shared-token
```

The script writes a JSON report under `data/runtime/platform-live-gate/` and
returns one of three decisions:

- `dual_surface_ready`
- `feishu_baseline_with_weixin_parity_track`
- `blocked`

Use the generated report like this:

- `feishu.rehearsal_ready` tells you whether the Feishu baseline surface passed the full rehearsal matrix
- `weixin.rehearsal_ready` tells you whether Weixin passed the same matrix
- `notification_replay` and `proactive_notification_replay` show source-bound versus proactive delivery outcomes
- `parity_ready=true` means both surfaces are ready and the system is `dual-surface ready`
- `decision_reason` and `weixin_blocker_category` explain why Weixin is still below parity when the baseline remains available
- `weixin.ingress_probe` records the latest real ingress attempt, while `weixin.latest_successful_ingress_probe` keeps the most recent historical success so stale proof does not hide a current blocker such as `weixin_waiting_for_private_text`

## Notification delivery endpoint

The gateway now exposes the IM-side notification contract endpoint:

- `POST /api/notifications/deliveries`

Current behavior:

- resolves outbound routes primarily through `destination.route_key`
- falls back to `destination.platform` plus `destination.id` or `destination.recipient` when no `route_key` is supplied
- uses a shared non-200 error envelope for request-rejection failures such as `ROUTE_NOT_FOUND`
- uses HTTP 200 delivery responses for accepted requests
- enforces `delivery.mode` field combinations
- stores outbound idempotency results by `delivery.idempotency_key`
- optional redacted gateway status is available at `GET /api/gateway/status`
- gateway status includes a redacted `delivery_observability` summary with source-bound versus proactive counts and queue/failure classification
- gateway status now includes a top-level redacted `weixin` summary with:
  - specific `blocker_category`
  - coarse `ingress_blocker_category`
  - `poll`
  - `delivery_observability`
- `release_v1.weixin_blocker_category` remains the coarse parity bucket, not the DNS-specific blocker code

Required request header for active v2.0 work:

```text
X-Contract-Version: 2.0
```

Optional service auth:

```powershell
$env:IM_AGENT_SERVICE_TOKEN='replace-me'
```

If that variable is set, callers must send:

```text
Authorization: Bearer replace-me
```

Minimal example:

```bash
curl -X POST http://127.0.0.1:8787/api/notifications/deliveries \
  -H "Content-Type: application/json" \
  -H "X-Contract-Version: 2.0" \
  -d '{
    "notification_id": "notif_001",
    "trace_id": "trace_001",
    "destination": {
      "kind": "conversation",
      "route_key": "gw_route_existing"
    },
    "content": {
      "title": "Front Door",
      "body": "1 person detected.",
      "payload_format": "plain_text",
      "structured_payload": {},
      "attachments": []
    },
    "delivery": {
      "mode": "send",
      "reply_to_message_id": "",
      "update_message_id": "",
      "idempotency_key": "idem_001"
    }
  }'
```

## Mobile setup portal

To let a user configure Feishu without logging into the server, the gateway now includes a small setup portal and QR entry.

Recommended startup for phone-based onboarding:

```powershell
$env:IM_AGENT_HOST='0.0.0.0'
$env:IM_AGENT_PORT='8787'
$env:IM_AGENT_PUBLIC_ORIGIN='http://192.168.3.10:8787'
python -m im_agent.server
```

Important notes:

- `IM_AGENT_HOST=0.0.0.0` lets devices on the same LAN reach the gateway
- `IM_AGENT_PUBLIC_ORIGIN` should be the exact URL your phone can open
- Feishu credentials entered here are stored only on the HarborGate machine

Useful routes:

- `GET /setup/qr` shows a desktop page with the QR code
- `GET /setup/qr.svg` returns the raw QR SVG
- `GET /setup` serves the mobile-friendly Feishu form
- `GET /api/setup/status` returns the current setup payload
- `POST /api/setup/feishu/configure` validates and hot-applies Feishu credentials

What the setup page does:

- shows the current Feishu credential status and long-connection state
- accepts `app_id`, `app_secret`, and optional `verification_token`
- validates the credentials against the Feishu Open Platform
- saves the credentials locally and hot-applies them to the running `FeishuAdapter`
- enables live send and defaults Feishu receive mode to websocket / long connection

After the page says validation succeeded, configure the Feishu developer console like this:

- switch event subscription mode to `Use long connection to receive events`
- subscribe `im.message.receive_v1`
- publish the app version

`IM_AGENT_PUBLIC_ORIGIN` is only for the phone setup page. It is not used as a Feishu callback URL in the default websocket flow.

## WeChat setup

This starter now includes a first-pass personal WeChat integration built around the recent iLink relay model that the Hermes/OpenClaw ecosystem has been using as of March-April 2026.

Current scope:

- QR login helper
- long polling via `getupdates`
- text inbound normalization
- text outbound replies with stored `context_token`
- persistent duplicate-update suppression in the runner
- private chats only for now

Not included yet:

- group chats
- image/file/voice send and receive
- webhook mode

### 1. Login once with QR

```powershell
harborgate-weixin-login
```

This stores the returned bot credentials under `data/weixin/accounts/`.

### 2. Set the account ID

After login succeeds, set the account ID printed by the login helper:

```powershell
$env:WEIXIN_ACCOUNT_ID='your-account-id'
```

Optional overrides:

```powershell
$env:WEIXIN_STATE_DIR='data/weixin'
$env:WEIXIN_BOT_TOKEN='override-token'
$env:WEIXIN_BASE_URL='https://ilinkai.weixin.qq.com'
```

Normally you only need `WEIXIN_ACCOUNT_ID`, because the token and base URL can be restored from the saved account file.

### 3. Start HarborGate

```powershell
harborgate
```

The `harborgate.service` process starts the Weixin runtime internally. It will:

1. long-poll WeChat updates
2. normalize private text messages into the gateway
3. generate a reply with the configured brain
4. send the reply back through WeChat using the cached `context_token`

Important:

- the user must send the bot a DM first, because the first inbound message provides the `context_token`
- if `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` are unset, replies come from the built-in demo brain
- if you want model-backed responses, set those variables before starting the runner

### 4. Confirm real provider-side ingress

Use the ingress probe when you need to distinguish between:

- `poll is healthy but no private text has arrived yet`
- `poll itself is failing`

```powershell
harborgate-weixin-ingress-probe
```

Or, without reinstalling scripts:

```powershell
python .\tools\run_weixin_ingress_probe.py
```

What to expect:

- `provider_private_text_seen=true` means HarborGate has observed a real provider-originated Weixin private text message
- `last_poll_outcome=idle_timeout` means long polling was healthy but idle; this is no longer treated as a transport failure
- `blocked_reason=waiting_for_private_text` means the account is restored and polling is healthy, but you still need to send one real DM from Weixin to complete the ingress proof

## Feishu transport

The codebase now includes a dedicated `FeishuAdapter` that follows the same separation we want across all IM platforms:

- gateway owns orchestration, sessions, and agent calls
- each IM owns its own adapter
- adapters translate platform payloads into `InboundMessage`
- adapters also own outbound request shapes for their platform
- the agent core does not need to know whether the message came from WeChat, Feishu, or something else

Current Feishu scope:

- runs Feishu in websocket / long-connection mode by default
- accepts Feishu-style webhook callbacks for `im.message.receive_v1` when explicitly switched to webhook mode
- handles `url_verification` challenge callbacks
- normalizes direct-message text, image, and card-action events into the internal model
- leaves group-message gates at the adapter boundary
- enforces explicit `@mention` for group events
- can send real text and native image messages through the Feishu Open Platform API when live send is enabled
- keeps an interactive-card send path for card-mode delivery
- supports mobile configuration through `/setup` and `/setup/qr`
- starts and stops the live Feishu transport from the unified `GatewayService`

Current Feishu limitations:

- no message update support yet

Recommended Feishu setup:

```powershell
$env:FEISHU_APP_ID='cli_xxx'
$env:FEISHU_APP_SECRET='secret_xxx'
$env:FEISHU_CONNECTION_MODE='websocket'
$env:FEISHU_ENABLE_LIVE_SEND='1'
```

Then:

- set Feishu event subscription mode to long connection
- subscribe `im.message.receive_v1`
- let the adapter deliver replies through the Feishu Open Platform API

Optional webhook mode:

```powershell
$env:FEISHU_CONNECTION_MODE='webhook'
$env:FEISHU_WEBHOOK_PATH='/feishu/webhook'
$env:FEISHU_VERIFICATION_TOKEN='verify-token'
```

Webhook mode should be used only when you already have a public HTTP endpoint for Feishu callbacks.

You can still exercise the adapter manually by POSTing a normalized payload to `/messages/feishu` once `FEISHU_APP_ID` and `FEISHU_APP_SECRET` are set:

```json
{
  "chat_id": "oc_demo_chat",
  "user_id": "ou_demo_user",
  "chat_type": "p2p",
  "text": "你好，飞书"
}
```

## How to extend to another IM platform

1. Create `src/im_agent/platforms/<platform>.py`
2. Subclass `PlatformAdapter`
3. Convert the platform payload into `InboundMessage`
4. Override `send_outbound()` if the platform can really deliver messages
5. Register the adapter in `build_default_gateway()`
6. Keep outbound delivery formatting inside the adapter, not in the agent core

That means we can later add Feishu, WeCom, Telegram, QQ Bot, or WhatsApp without rewriting the gateway loop.

## Why this is a good starting point

- small enough to understand in one sitting
- compatible with local demos from day one
- structured like a production gateway, so it can grow without a rewrite
- easy to replace the storage layer, auth layer, and LLM backend later

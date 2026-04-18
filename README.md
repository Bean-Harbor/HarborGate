# IM Agent Starter

This repository is a clean-room starter project for building our own IM-connected agent service.

It is inspired by the architecture of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent), especially these ideas:

- keep the agent core separate from messaging platform adapters
- normalize inbound events into one internal message model
- store sessions per chat instead of coupling memory to a single frontend
- make the LLM backend swappable

This project does not copy Hermes source code. It borrows the architecture direction and re-implements a much smaller starter in our own structure.

## Project governance

The project is now pinned to [`HarborNAS-IM-Gateway-Agent-Contract-v1.5.md`](./HarborNAS-IM-Gateway-Agent-Contract-v1.5.md) as the working cross-repo implementation guide.

Management documents:

- [`ROADMAP.md`](./ROADMAP.md)
- [`PLAN.md`](./PLAN.md)
- [`WORKLOG.md`](./WORKLOG.md)

## What is included

- a `GatewayService` that routes inbound platform events
- a `PlatformAdapter` abstraction for IM adapters
- a small adapter registry so platforms are plugged into one gateway flow
- a generic `WebhookAdapter` that we can use immediately
- a first-pass `WeixinAdapter` for personal WeChat text messages
- a `FeishuAdapter` with websocket-first receive mode and real text send
- a file-based session store
- a default rule-based brain for local testing
- an optional OpenAI-compatible backend through environment variables
- a tiny HTTP server using the Python standard library

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

## HarborNAS task API mode

If `HARBORNAS_TASK_API_URL` is set, the gateway sends inbound turns to HarborNAS through the frozen `v1.5` task contract instead of using the local demo brain.

```powershell
$env:HARBORNAS_TASK_API_URL='http://127.0.0.1:9000'
$env:HARBORNAS_TASK_API_TOKEN='replace-me'
$env:HARBORNAS_CONTRACT_VERSION='1.5'
$env:HARBORNAS_DEFAULT_DOMAIN='general'
$env:HARBORNAS_DEFAULT_ACTION='message'
$env:HARBORNAS_AUTONOMY_LEVEL='supervised'
```

Behavior in this mode:

- the gateway builds canonical `POST /api/tasks` requests
- stable `task_id` and `trace_id` are derived from inbound event identity
- `route_key` and `session_id` are generated when the adapter does not provide them
- `resume_token` is stored per chat and sent back on the next follow-up turn
- HarborNAS `TaskResponse` content is mapped back into the adapter delivery path

If `HARBORNAS_TASK_API_URL` is unset, the gateway falls back to the local rule-based brain or the OpenAI-compatible backend.

## Notification delivery endpoint

The gateway now exposes the IM-side notification contract endpoint:

- `POST /api/notifications/deliveries`

Current behavior:

- resolves outbound routes primarily through `destination.route_key`
- uses a shared non-200 error envelope for request-rejection failures such as `ROUTE_NOT_FOUND`
- uses HTTP 200 delivery responses for accepted requests
- enforces `delivery.mode` field combinations
- stores outbound idempotency results by `delivery.idempotency_key`

Required request header:

```text
X-Contract-Version: 1.5
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
  -H "X-Contract-Version: 1.5" \
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
- Feishu credentials entered here are stored only on the IM Gateway machine

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
im-agent-weixin-login
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

### 3. Start the WeChat runner

```powershell
im-agent-weixin-runner
```

The runner will:

1. long-poll WeChat updates
2. normalize private text messages into the gateway
3. generate a reply with the configured brain
4. send the reply back through WeChat using the cached `context_token`

Important:

- the user must send the bot a DM first, because the first inbound message provides the `context_token`
- if `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` are unset, replies come from the built-in demo brain
- if you want model-backed responses, set those variables before starting the runner

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
- normalizes direct-message text events into the internal model
- leaves group-message gates at the adapter boundary
- enforces explicit `@mention` for group events
- can send real text messages through the Feishu Open Platform API when live send is enabled
- supports mobile configuration through `/setup` and `/setup/qr`
- starts and stops the live Feishu transport from the unified `GatewayService`

Current Feishu limitations:

- no media/card/reaction handling yet
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

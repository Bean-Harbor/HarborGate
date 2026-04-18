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
- a `FeishuAdapter` skeleton for Feishu / Lark protocol translation
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
    feishu.py         # Feishu / Lark protocol adapter skeleton
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

## WeChat setup

This starter now includes a first-pass personal WeChat integration built around the recent iLink relay model that the Hermes/OpenClaw ecosystem has been using as of March-April 2026.

Current scope:

- QR login helper
- long polling via `getupdates`
- text inbound normalization
- text outbound replies with stored `context_token`
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

## Feishu architecture

The codebase now includes a dedicated `FeishuAdapter` skeleton that follows the same separation we want across all IM platforms:

- gateway owns orchestration, sessions, and agent calls
- each IM owns its own adapter
- adapters translate platform payloads into `InboundMessage`
- adapters also own outbound request shapes for their platform
- the agent core does not need to know whether the message came from WeChat, Feishu, or something else

Current Feishu scope:

- reads Feishu-style raw webhook events for `im.message.receive_v1`
- normalizes direct-message text events into the internal model
- leaves group-message gates at the adapter boundary
- enforces explicit `@mention` for group events
- builds Feishu text-send request payloads for future transport wiring

Current Feishu limitations:

- no live websocket client yet
- no webhook server yet
- no media/card/reaction handling yet
- outbound delivery is still a skeleton payload, not a real API call

If you want to exercise the skeleton manually, you can POST a normalized payload to `/messages/feishu` once `FEISHU_APP_ID` and `FEISHU_APP_SECRET` are set:

```json
{
  "chat_id": "oc_demo_chat",
  "user_id": "ou_demo_user",
  "chat_type": "p2p",
  "text": "你好，飞书"
}
```

## How to extend to a real IM platform

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

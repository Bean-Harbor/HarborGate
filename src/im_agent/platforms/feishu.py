from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib import error, parse, request

from im_agent.models import InboundMessage, OutboundMessage
from im_agent.platforms.base import PlatformAdapter

logger = logging.getLogger(__name__)

DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn"
FEISHU_BOT_INFO_ENDPOINT = "/open-apis/bot/v3/info"
FEISHU_MESSAGE_EVENT_TYPE = "im.message.receive_v1"

InboundHandler = Callable[[dict[str, Any]], None]


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(slots=True)
class FeishuSettings:
    app_id: str
    app_secret: str
    domain: str = "feishu"
    connection_mode: str = "websocket"
    allowed_users: set[str] = field(default_factory=set)
    group_policy: str = "allowlist"
    bot_open_id: str = ""
    bot_user_id: str = ""
    bot_name: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8765
    webhook_path: str = "/feishu/webhook"
    base_url: str = DEFAULT_FEISHU_BASE_URL
    auth_base_url: str = DEFAULT_FEISHU_BASE_URL
    enable_live_send: bool = False
    timeout_seconds: int = 20


def parse_csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def build_feishu_settings_from_env() -> FeishuSettings:
    return FeishuSettings(
        app_id=os.getenv("FEISHU_APP_ID", "").strip(),
        app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
        domain=(os.getenv("FEISHU_DOMAIN", "feishu").strip() or "feishu").lower(),
        connection_mode=(os.getenv("FEISHU_CONNECTION_MODE", "websocket").strip() or "websocket").lower(),
        allowed_users=parse_csv_set(os.getenv("FEISHU_ALLOWED_USERS", "")),
        group_policy=(os.getenv("FEISHU_GROUP_POLICY", "allowlist").strip() or "allowlist").lower(),
        bot_open_id=os.getenv("FEISHU_BOT_OPEN_ID", "").strip(),
        bot_user_id=os.getenv("FEISHU_BOT_USER_ID", "").strip(),
        bot_name=os.getenv("FEISHU_BOT_NAME", "").strip(),
        verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip(),
        encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", "").strip(),
        webhook_host=os.getenv("FEISHU_WEBHOOK_HOST", "127.0.0.1").strip() or "127.0.0.1",
        webhook_port=int(os.getenv("FEISHU_WEBHOOK_PORT", "8765")),
        webhook_path=os.getenv("FEISHU_WEBHOOK_PATH", "/feishu/webhook").strip() or "/feishu/webhook",
        base_url=os.getenv("FEISHU_BASE_URL", DEFAULT_FEISHU_BASE_URL).strip() or DEFAULT_FEISHU_BASE_URL,
        auth_base_url=os.getenv("FEISHU_AUTH_BASE_URL", DEFAULT_FEISHU_BASE_URL).strip() or DEFAULT_FEISHU_BASE_URL,
        enable_live_send=os.getenv("FEISHU_ENABLE_LIVE_SEND", "").strip().lower() in {"1", "true", "yes", "on"},
        timeout_seconds=int(os.getenv("FEISHU_TIMEOUT_SECONDS", "20")),
    )


def parse_feishu_message_content(raw_content: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw_content, dict):
        return raw_content
    text = str(raw_content or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return payload if isinstance(payload, dict) else {}


def build_feishu_text_payload(chat_id: str, text: str) -> dict[str, Any]:
    return {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }


class FeishuWebsocketRuntime:
    def start(self) -> None:
        raise NotImplementedError

    def stop(self, timeout_seconds: float = 5.0) -> None:
        raise NotImplementedError


class OfficialFeishuWebsocketRuntime(FeishuWebsocketRuntime):
    def __init__(
        self,
        *,
        settings: FeishuSettings,
        on_event: InboundHandler,
        on_connected: Callable[[], None],
        on_disconnected: Callable[[], None],
    ) -> None:
        self.settings = settings
        self.on_event = on_event
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self._stop_requested = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None

    def start(self) -> None:
        from lark_oapi import EventDispatcherHandler, LogLevel
        from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
        from lark_oapi.ws import Client as LarkWSClient
        import lark_oapi.ws.client as ws_client_module

        runtime = self

        class ManagedLarkWSClient(LarkWSClient):
            def __init__(
                self,
                *,
                app_id: str,
                app_secret: str,
                event_handler: Any,
                domain: str,
            ) -> None:
                super().__init__(
                    app_id=app_id,
                    app_secret=app_secret,
                    log_level=LogLevel.INFO,
                    event_handler=event_handler,
                    domain=domain,
                    auto_reconnect=True,
                )

            async def _connect(self) -> None:
                await super()._connect()
                if self._conn is not None:
                    runtime.on_connected()

            async def _disconnect(self) -> None:
                was_connected = self._conn is not None
                await super()._disconnect()
                if was_connected:
                    runtime.on_disconnected()

            def stop(self, timeout_seconds: float = 5.0) -> None:
                self._auto_reconnect = False
                loop = ws_client_module.loop
                if loop.is_closed():
                    return
                try:
                    future = asyncio.run_coroutine_threadsafe(self._disconnect(), loop)
                    future.result(timeout=timeout_seconds)
                except Exception:
                    logger.debug("Feishu websocket disconnect wait timed out", exc_info=True)
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)

        def handle_message(data: P2ImMessageReceiveV1) -> None:
            runtime.on_event(_sdk_event_to_payload(data))

        dispatcher = (
            EventDispatcherHandler.builder(
                self.settings.encrypt_key,
                self.settings.verification_token,
                LogLevel.INFO,
            )
            .register_p2_im_message_receive_v1(handle_message)
            .build()
        )

        loop = asyncio.new_event_loop()
        ws_client_module.loop = loop
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._client = ManagedLarkWSClient(
            app_id=self.settings.app_id,
            app_secret=self.settings.app_secret,
            event_handler=dispatcher,
            domain=self.settings.base_url.rstrip("/"),
        )

        try:
            self._client.start()
        except RuntimeError as exc:
            if self._stop_requested.is_set() and "Event loop stopped before Future completed" in str(exc):
                return
            raise
        finally:
            self._client = None
            try:
                if not loop.is_closed():
                    loop.close()
            finally:
                self._loop = None

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop_requested.set()
        client = self._client
        if client is not None and hasattr(client, "stop"):
            client.stop(timeout_seconds=timeout_seconds)
            return
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)


def _sdk_event_to_payload(data: Any) -> dict[str, Any]:
    event = getattr(data, "event", None)
    if event is None:
        raise ValueError("Feishu websocket event is missing event payload")

    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    mentions_payload: list[dict[str, Any]] = []
    for mention in getattr(message, "mentions", None) or []:
        mention_id = getattr(mention, "id", None)
        mentions_payload.append(
            {
                "key": str(getattr(mention, "key", "") or ""),
                "name": str(getattr(mention, "name", "") or ""),
                "tenant_key": str(getattr(mention, "tenant_key", "") or ""),
                "id": {
                    "open_id": str(getattr(mention_id, "open_id", "") or ""),
                    "user_id": str(getattr(mention_id, "user_id", "") or ""),
                },
            }
        )

    return {
        "header": {
            "event_type": FEISHU_MESSAGE_EVENT_TYPE,
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": str(getattr(sender_id, "open_id", "") or ""),
                    "user_id": str(getattr(sender_id, "user_id", "") or ""),
                },
                "sender_type": str(getattr(sender, "sender_type", "") or ""),
                "tenant_key": str(getattr(sender, "tenant_key", "") or ""),
            },
            "message": {
                "message_id": str(getattr(message, "message_id", "") or ""),
                "chat_id": str(getattr(message, "chat_id", "") or ""),
                "chat_type": str(getattr(message, "chat_type", "") or "p2p"),
                "message_type": str(getattr(message, "message_type", "") or "text"),
                "content": str(getattr(message, "content", "") or ""),
                "mentions": mentions_payload,
                "root_id": str(getattr(message, "root_id", "") or ""),
                "parent_id": str(getattr(message, "parent_id", "") or ""),
                "thread_id": str(getattr(message, "thread_id", "") or ""),
                "create_time": getattr(message, "create_time", None),
                "update_time": getattr(message, "update_time", None),
            },
        },
    }


class FeishuAdapter(PlatformAdapter):
    """Feishu / Lark adapter with websocket-first transport and optional webhook mode."""

    name = "feishu"

    def __init__(
        self,
        settings: FeishuSettings | None = None,
        *,
        websocket_runtime_factory: Callable[..., FeishuWebsocketRuntime] | None = None,
    ) -> None:
        self.settings = settings or build_feishu_settings_from_env()
        self._tenant_access_token = ""
        self._tenant_access_token_expires_at = 0.0
        self._inbound_handler: InboundHandler | None = None
        self._transport_lock = threading.Lock()
        self._websocket_thread: threading.Thread | None = None
        self._websocket_runtime: FeishuWebsocketRuntime | None = None
        self._disconnect_requested = False
        self._websocket_runtime_factory = websocket_runtime_factory or OfficialFeishuWebsocketRuntime
        self._transport_state: dict[str, Any] = {
            "mode": self.settings.connection_mode,
            "status": "waiting_for_credentials" if not self.configured else f"{self.settings.connection_mode}_idle",
            "connected": False,
            "last_error": "",
            "last_connected_at": "",
            "last_event_at": "",
        }

    def apply_settings(self, settings: FeishuSettings) -> None:
        previous_mode = self.settings.connection_mode
        previous_identity = (
            self.settings.app_id,
            self.settings.app_secret,
            self.settings.base_url,
            self.settings.auth_base_url,
        )
        next_identity = (
            settings.app_id,
            settings.app_secret,
            settings.base_url,
            settings.auth_base_url,
        )
        self.settings = settings
        self._tenant_access_token = ""
        self._tenant_access_token_expires_at = 0.0
        self._set_transport_state(
            mode=self.settings.connection_mode,
            status="waiting_for_credentials" if not self.configured else f"{self.settings.connection_mode}_idle",
            connected=False,
            last_error="",
        )
        if self._inbound_handler is None:
            return
        if previous_mode != settings.connection_mode or previous_identity != next_identity:
            self.disconnect()
        self.connect(self._inbound_handler)

    @property
    def configured(self) -> bool:
        return bool(self.settings.app_id and self.settings.app_secret)

    @property
    def webhook_path(self) -> str:
        return self.settings.webhook_path

    def connect(self, inbound_handler: InboundHandler) -> None:
        self._inbound_handler = inbound_handler
        if not self.configured:
            self._set_transport_state(status="waiting_for_credentials", connected=False)
            return
        if self.settings.connection_mode != "websocket":
            self._set_transport_state(status="webhook_idle", connected=False)
            return

        with self._transport_lock:
            if self._websocket_thread is not None and self._websocket_thread.is_alive():
                return
            self._disconnect_requested = False
            runtime = self._websocket_runtime_factory(
                settings=self.settings,
                on_event=self._handle_websocket_event,
                on_connected=self._mark_websocket_connected,
                on_disconnected=self._mark_websocket_disconnected,
            )
            thread = threading.Thread(
                target=self._run_websocket_transport,
                args=(runtime,),
                daemon=True,
                name="im-agent-feishu-ws",
            )
            self._websocket_runtime = runtime
            self._websocket_thread = thread
        self._set_transport_state(status="connecting", connected=False, last_error="")
        thread.start()

    def disconnect(self) -> None:
        with self._transport_lock:
            runtime = self._websocket_runtime
            thread = self._websocket_thread
            self._disconnect_requested = True

        if runtime is not None:
            runtime.stop()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

        with self._transport_lock:
            self._websocket_runtime = None
            self._websocket_thread = None
        next_status = "waiting_for_credentials" if not self.configured else f"{self.settings.connection_mode}_idle"
        self._set_transport_state(status=next_status, connected=False)

    def transport_status(self) -> dict[str, Any]:
        with self._transport_lock:
            state = dict(self._transport_state)
            state["thread_alive"] = bool(self._websocket_thread and self._websocket_thread.is_alive())
        return state

    def is_url_verification(self, payload: dict[str, Any]) -> bool:
        return str(payload.get("type") or "").strip() == "url_verification"

    def build_url_verification_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.validate_callback(payload)
        challenge = str(payload.get("challenge") or "").strip()
        if not challenge:
            raise ValueError("Feishu url_verification payload is missing challenge")
        return {"challenge": challenge}

    def validate_callback(self, payload: dict[str, Any]) -> None:
        expected = self.settings.verification_token.strip()
        if not expected:
            return
        header = payload.get("header")
        if "token" not in payload and not (isinstance(header, dict) and "token" in header) and not self.is_url_verification(payload):
            return
        received = str(payload.get("token") or (header or {}).get("token") or "").strip()
        if received != expected:
            raise ValueError("Feishu callback token validation failed")

    def normalize_inbound(self, payload: dict[str, Any]) -> InboundMessage:
        self.validate_callback(payload)
        if "header" in payload and "event" in payload:
            return self._normalize_raw_event(payload)
        return self._normalize_compact_payload(payload)

    def build_delivery_payload(self, outbound: OutboundMessage) -> dict[str, Any]:
        request_payload = self._build_request_body(outbound)
        note = (
            "Feishu live send is disabled. Set FEISHU_ENABLE_LIVE_SEND=1 to send through "
            "the Feishu Open Platform API."
        )
        if self.settings.connection_mode == "websocket":
            note = (
                "Feishu is configured for websocket/long-connection receive mode. "
                "Outbound delivery still uses the Feishu Open Platform API."
            )
        return {
            **outbound.to_dict(),
            "delivery": "feishu",
            "sent": False,
            "connection_mode": self.settings.connection_mode,
            "domain": self.settings.domain,
            "request": request_payload,
            "note": note,
        }

    def send_outbound(self, outbound: OutboundMessage) -> dict[str, Any]:
        if not (self.configured and self.settings.enable_live_send):
            return self.build_delivery_payload(outbound)

        response = self._send_message_request(outbound)
        message_id = str(((response.get("data") or {}).get("message_id") or "")).strip()
        return {
            "platform": "feishu",
            "chat_id": outbound.chat_id,
            "text": outbound.text,
            "timestamp": outbound.timestamp,
            "delivery": "feishu",
            "sent": True,
            "message_id": message_id,
            "metadata": {
                **outbound.metadata,
                "connection_mode": self.settings.connection_mode,
            },
            "request": self._build_request_body(outbound),
            "response": response,
        }

    def fetch_bot_info(self) -> dict[str, Any]:
        response = self._get_json(
            self.settings.base_url,
            FEISHU_BOT_INFO_ENDPOINT,
            token=self._get_tenant_access_token(),
        )
        data = response.get("data")
        return dict(data) if isinstance(data, dict) else {}

    def _run_websocket_transport(self, runtime: FeishuWebsocketRuntime) -> None:
        try:
            runtime.start()
        except Exception as exc:
            logger.exception("Feishu websocket transport exited unexpectedly")
            self._set_transport_state(status="error", connected=False, last_error=str(exc))
        finally:
            with self._transport_lock:
                if self._websocket_runtime is runtime:
                    self._websocket_runtime = None
                self._websocket_thread = None
            if not self._disconnect_requested and self.settings.connection_mode == "websocket" and self.configured:
                current = self.transport_status()
                if not current.get("connected"):
                    self._set_transport_state(status="disconnected", connected=False)

    def _handle_websocket_event(self, payload: dict[str, Any]) -> None:
        self._set_transport_state(status="connected", connected=True, last_event_at=_now_utc(), last_error="")
        if self._inbound_handler is None:
            raise RuntimeError("Feishu websocket event arrived before inbound handler was registered")
        self._inbound_handler(payload)

    def _mark_websocket_connected(self) -> None:
        self._set_transport_state(
            status="connected",
            connected=True,
            last_connected_at=_now_utc(),
            last_error="",
        )

    def _mark_websocket_disconnected(self) -> None:
        next_status = "websocket_idle" if self._disconnect_requested else "reconnecting"
        self._set_transport_state(status=next_status, connected=False)

    def _set_transport_state(self, **updates: Any) -> None:
        with self._transport_lock:
            self._transport_state.update(updates)
            self._transport_state["mode"] = self.settings.connection_mode

    def _build_request_body(self, outbound: OutboundMessage) -> dict[str, Any]:
        reply_to_message_id = str(outbound.metadata.get("reply_to_message_id") or "").strip()
        if reply_to_message_id:
            return {
                "content": json.dumps({"text": outbound.text}, ensure_ascii=False),
                "msg_type": "text",
            }
        return build_feishu_text_payload(outbound.chat_id, outbound.text)

    def _send_message_request(self, outbound: OutboundMessage) -> dict[str, Any]:
        body = self._build_request_body(outbound)
        reply_to_message_id = str(outbound.metadata.get("reply_to_message_id") or "").strip()
        update_message_id = str(outbound.metadata.get("update_message_id") or "").strip()
        if update_message_id:
            raise RuntimeError("Feishu message update is not supported in this starter yet")

        if reply_to_message_id:
            endpoint = f"/open-apis/im/v1/messages/{parse.quote(reply_to_message_id)}/reply"
        else:
            endpoint = "/open-apis/im/v1/messages?receive_id_type=chat_id"

        return self._post_json(
            self.settings.base_url,
            endpoint,
            body,
            token=self._get_tenant_access_token(),
        )

    def _get_tenant_access_token(self) -> str:
        if self._tenant_access_token and time.time() < self._tenant_access_token_expires_at:
            return self._tenant_access_token

        response = self._post_json(
            self.settings.auth_base_url,
            "/open-apis/auth/v3/tenant_access_token/internal",
            {
                "app_id": self.settings.app_id,
                "app_secret": self.settings.app_secret,
            },
            token="",
        )
        tenant_access_token = str(response.get("tenant_access_token") or "").strip()
        expire = int(response.get("expire") or 0)
        if not tenant_access_token:
            raise RuntimeError("Feishu tenant_access_token response did not include tenant_access_token")
        ttl_seconds = max(60, expire - 60) if expire else 60
        self._tenant_access_token = tenant_access_token
        self._tenant_access_token_expires_at = time.time() + ttl_seconds
        return tenant_access_token

    def _post_json(self, base_url: str, endpoint: str, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._request_json("POST", base_url, endpoint, payload_bytes=body, token=token)

    def _get_json(self, base_url: str, endpoint: str, *, token: str) -> dict[str, Any]:
        return self._request_json("GET", base_url, endpoint, payload_bytes=None, token=token)

    def _request_json(
        self,
        method: str,
        base_url: str,
        endpoint: str,
        *,
        payload_bytes: bytes | None,
        token: str,
    ) -> dict[str, Any]:
        req = request.Request(
            parse.urljoin(f"{base_url.rstrip('/')}/", endpoint.lstrip("/")),
            data=payload_bytes,
            headers=self._headers(token, payload_bytes),
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.settings.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu API returned HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Could not reach Feishu API: {exc.reason}") from exc

        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise RuntimeError("Feishu API returned a non-object JSON payload")
        if int(data.get("code") or 0) != 0:
            raise RuntimeError(
                f"Feishu API returned code {data.get('code')}: {data.get('msg') or data.get('message') or 'unknown error'}"
            )
        return data

    @staticmethod
    def _headers(token: str, body: bytes | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _normalize_compact_payload(self, payload: dict[str, Any]) -> InboundMessage:
        chat_id = str(payload.get("chat_id") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        chat_type = str(payload.get("chat_type") or "p2p").strip().lower()
        message_id = str(payload.get("message_id") or "").strip()
        route_key = str(payload.get("route_key") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        mentions = payload.get("mentions") or []
        attachments = payload.get("attachments") or []
        raw_content = str(payload.get("raw_content") or text)

        if not chat_id:
            raise ValueError("Feishu payload must include chat_id")
        if not user_id:
            raise ValueError("Feishu payload must include user_id")
        if not text:
            raise ValueError("Feishu payload must include text")

        self._enforce_access_policy(
            chat_type=chat_type,
            sender_open_id=user_id,
            raw_content=raw_content,
            mentions=mentions,
        )

        return InboundMessage(
            platform="feishu",
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            message_id=message_id,
            chat_type=chat_type,
            route_key=route_key,
            session_id=session_id,
            mentions=[item for item in mentions if isinstance(item, dict)],
            attachments=[item for item in attachments if isinstance(item, dict)],
            raw_payload=payload,
        )

    def _normalize_raw_event(self, payload: dict[str, Any]) -> InboundMessage:
        header = payload.get("header") or {}
        event_type = str(header.get("event_type") or "").strip()
        if event_type != FEISHU_MESSAGE_EVENT_TYPE:
            raise ValueError(f"Unsupported Feishu event_type: {event_type or 'unknown'}")

        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        sender_open_id = str(sender_id.get("open_id") or sender_id.get("user_id") or "").strip()
        chat_id = str(message.get("chat_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        chat_type = str(message.get("chat_type") or "p2p").strip().lower()
        message_type = str(message.get("message_type") or "").strip().lower()
        if message_type != "text":
            raise ValueError(f"Unsupported Feishu message_type for this starter: {message_type or 'unknown'}")

        content = parse_feishu_message_content(message.get("content") or "")
        text = str(content.get("text") or "").strip()
        mentions = message.get("mentions") or []

        if not sender_open_id:
            raise ValueError("Feishu event is missing sender open_id/user_id")
        if not chat_id:
            raise ValueError("Feishu event is missing chat_id")
        if not text:
            raise ValueError("Feishu text message is empty")

        self._enforce_access_policy(
            chat_type=chat_type,
            sender_open_id=sender_open_id,
            raw_content=str(message.get("content") or ""),
            mentions=mentions,
        )

        return InboundMessage(
            platform="feishu",
            chat_id=chat_id,
            user_id=sender_open_id,
            text=text,
            message_id=message_id,
            chat_type=chat_type,
            mentions=[item for item in mentions if isinstance(item, dict)],
            raw_payload=payload,
        )

    def _enforce_access_policy(
        self,
        *,
        chat_type: str,
        sender_open_id: str,
        raw_content: str,
        mentions: list[Any],
    ) -> None:
        if self.settings.allowed_users and sender_open_id not in self.settings.allowed_users:
            raise ValueError("Feishu sender is not in FEISHU_ALLOWED_USERS")

        if chat_type == "p2p":
            return

        if self.settings.group_policy == "disabled":
            raise ValueError("Feishu group messages are disabled by FEISHU_GROUP_POLICY")

        if not self._message_mentions_bot(raw_content=raw_content, mentions=mentions):
            raise ValueError("Feishu group messages must explicitly @mention the bot")

    def _message_mentions_bot(self, *, raw_content: str, mentions: list[Any]) -> bool:
        if "@_all" in raw_content:
            return True

        if not mentions:
            return False

        for mention in mentions:
            mention_id = mention.get("id") if isinstance(mention, dict) else getattr(mention, "id", None)
            mention_name = mention.get("name") if isinstance(mention, dict) else getattr(mention, "name", "")

            if isinstance(mention_id, dict):
                mention_open_id = str(mention_id.get("open_id") or "").strip()
                mention_user_id = str(mention_id.get("user_id") or "").strip()
            else:
                mention_open_id = str(getattr(mention_id, "open_id", "") or "").strip()
                mention_user_id = str(getattr(mention_id, "user_id", "") or "").strip()

            if self.settings.bot_open_id and mention_open_id == self.settings.bot_open_id:
                return True
            if self.settings.bot_user_id and mention_user_id == self.settings.bot_user_id:
                return True
            if self.settings.bot_name and str(mention_name or "").strip() == self.settings.bot_name:
                return True

        return False

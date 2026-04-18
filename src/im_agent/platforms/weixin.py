from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from im_agent.models import InboundMessage, OutboundMessage
from im_agent.platforms.base import PlatformAdapter

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (2 << 8) | 0)

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

ITEM_TEXT = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_POLL_TIMEOUT_MS = 35_000
MAX_TEXT_CHUNK_LENGTH = 900


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(value: str) -> str:
    text = (value or "").strip() or "default"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)


def _account_dir(state_dir: str | Path) -> Path:
    path = Path(state_dir) / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _account_file(state_dir: str | Path, account_id: str) -> Path:
    return _account_dir(state_dir) / f"{_safe_slug(account_id)}.json"


def _sync_file(state_dir: str | Path, account_id: str) -> Path:
    return _account_dir(state_dir) / f"{_safe_slug(account_id)}.sync.json"


def _context_file(state_dir: str | Path, account_id: str) -> Path:
    return _account_dir(state_dir) / f"{_safe_slug(account_id)}.context_tokens.json"


def _processed_file(state_dir: str | Path, account_id: str) -> Path:
    return _account_dir(state_dir) / f"{_safe_slug(account_id)}.processed_messages.json"


def extract_weixin_message_id(payload: dict[str, Any]) -> str:
    return str(payload.get("msg_id") or payload.get("client_id") or "").strip()


def save_weixin_account(
    state_dir: str | Path,
    *,
    account_id: str,
    token: str,
    base_url: str,
    user_id: str = "",
) -> None:
    payload = {
        "account_id": account_id,
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": utc_now_iso(),
    }
    path = _account_file(state_dir, account_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_weixin_account(state_dir: str | Path, account_id: str) -> dict[str, Any] | None:
    path = _account_file(state_dir, account_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


class ContextTokenStore:
    def __init__(self, state_dir: str | Path, account_id: str) -> None:
        self.path = _context_file(state_dir, account_id)
        self._cache: dict[str, str] = {}
        self._restore()

    def get(self, chat_id: str) -> str | None:
        return self._cache.get(chat_id)

    def set(self, chat_id: str, token: str) -> None:
        if not chat_id or not token:
            return
        self._cache[chat_id] = token
        self._persist()

    def _restore(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            self._cache = {str(key): str(value) for key, value in data.items() if str(value).strip()}

    def _persist(self) -> None:
        self.path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class ProcessedMessageStore:
    def __init__(self, state_dir: str | Path, account_id: str, max_items: int = 500) -> None:
        self.path = _processed_file(state_dir, account_id)
        self.max_items = max_items
        self._items: list[str] = []
        self._restore()

    def contains(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self._items

    def add(self, message_id: str) -> None:
        if not message_id or message_id in self._items:
            return
        self._items.append(message_id)
        if self.max_items > 0:
            self._items = self._items[-self.max_items :]
        self._persist()

    def _restore(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            self._items = [str(item) for item in data if str(item).strip()]

    def _persist(self) -> None:
        self.path.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_sync_buf(state_dir: str | Path, account_id: str) -> str:
    path = _sync_file(state_dir, account_id)
    if not path.exists():
        return ""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload.get("get_updates_buf", ""))


def save_sync_buf(state_dir: str | Path, account_id: str, sync_buf: str) -> None:
    path = _sync_file(state_dir, account_id)
    path.write_text(
        json.dumps({"get_updates_buf": sync_buf}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def split_text_for_weixin(text: str, max_length: int = MAX_TEXT_CHUNK_LENGTH) -> list[str]:
    content = text.strip()
    if not content:
        return []
    if len(content) <= max_length:
        return [content]

    chunks: list[str] = []
    current = ""
    for line in content.splitlines(keepends=True):
        if len(current) + len(line) <= max_length:
            current += line
            continue
        if current:
            chunks.append(current.rstrip())
            current = ""
        while len(line) > max_length:
            chunks.append(line[:max_length].rstrip())
            line = line[max_length:]
        current = line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [content[:max_length]]


def extract_text_from_item_list(item_list: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in item_list:
        if int(item.get("type") or 0) != ITEM_TEXT:
            continue
        text_item = item.get("text_item") or {}
        text = str(text_item.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def build_send_message_payload(
    *,
    to_user_id: str,
    text: str,
    context_token: str | None,
    client_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id or f"harborgate-{uuid.uuid4().hex}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
    }
    if context_token:
        payload["msg"]["context_token"] = context_token
    return payload


def _headers(token: str | None, body: bytes | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Length"] = str(len(body))
    return headers


def get_json(base_url: str, endpoint: str, token: str | None = None, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    url = parse.urljoin(f"{base_url.rstrip('/')}/", endpoint)
    req = request.Request(url, headers=_headers(token), method="GET")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Weixin GET {endpoint} failed with HTTP {exc.code}: {detail}") from exc


def post_json(
    base_url: str,
    endpoint: str,
    payload: dict[str, Any],
    *,
    token: str | None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    url = parse.urljoin(f"{base_url.rstrip('/')}/", endpoint)
    req = request.Request(
        url,
        data=body,
        headers=_headers(token, body),
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Weixin POST {endpoint} failed with HTTP {exc.code}: {detail}") from exc


@dataclass(slots=True)
class QRLoginResult:
    account_id: str
    token: str
    base_url: str
    user_id: str = ""


def run_weixin_qr_login(
    state_dir: str | Path = "data/weixin",
    *,
    bot_type: str = "3",
    timeout_seconds: int = 480,
) -> QRLoginResult | None:
    qr_resp = get_json(ILINK_BASE_URL, f"{EP_GET_BOT_QR}?bot_type={parse.quote(bot_type)}", token=None)
    qrcode_value = str(qr_resp.get("qrcode") or "").strip()
    qrcode_url = str(qr_resp.get("qrcode_img_content") or "").strip()
    if not qrcode_value:
        raise RuntimeError("Weixin QR response did not include qrcode")

    print("请使用微信扫描下面的二维码链接：")
    print(qrcode_url or qrcode_value)

    current_base_url = ILINK_BASE_URL
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            status = get_json(
                current_base_url,
                f"{EP_GET_QR_STATUS}?qrcode={parse.quote(qrcode_value)}",
                token=None,
            )
        except RuntimeError as exc:
            print(f"轮询二维码状态失败，1 秒后重试: {exc}")
            time.sleep(1)
            continue

        qr_status = str(status.get("status") or "wait")
        if qr_status == "wait":
            print(".", end="", flush=True)
        elif qr_status == "scaned":
            print("\n已扫码，请在微信中确认授权。")
        elif qr_status == "scaned_but_redirect":
            redirect_host = str(status.get("redirect_host") or "").strip()
            if redirect_host:
                current_base_url = f"https://{redirect_host}"
        elif qr_status == "expired":
            print("\n二维码已过期，请重新执行登录命令。")
            return None
        elif qr_status == "confirmed":
            result = QRLoginResult(
                account_id=str(status.get("ilink_bot_id") or "").strip(),
                token=str(status.get("bot_token") or "").strip(),
                base_url=str(status.get("baseurl") or ILINK_BASE_URL).strip(),
                user_id=str(status.get("ilink_user_id") or "").strip(),
            )
            if not result.account_id or not result.token:
                raise RuntimeError("Weixin confirmed login but the credential payload was incomplete")
            save_weixin_account(
                state_dir,
                account_id=result.account_id,
                token=result.token,
                base_url=result.base_url,
                user_id=result.user_id,
            )
            print(f"\n微信连接成功，account_id={result.account_id}")
            return result
        time.sleep(1)

    print("\n微信登录超时。")
    return None


class WeixinAdapter(PlatformAdapter):
    """Minimal personal WeChat adapter built around the iLink bot relay."""

    name = "weixin"

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        account_id: str | None = None,
        token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir or os.getenv("WEIXIN_STATE_DIR", "data/weixin"))
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.account_id = (account_id or os.getenv("WEIXIN_ACCOUNT_ID", "")).strip()
        self.token = (token or os.getenv("WEIXIN_BOT_TOKEN", "")).strip()
        self.base_url = (base_url or os.getenv("WEIXIN_BASE_URL", ILINK_BASE_URL)).strip() or ILINK_BASE_URL
        self.user_id = os.getenv("WEIXIN_USER_ID", "").strip()

        if self.account_id and not self.token:
            saved = load_weixin_account(self.state_dir, self.account_id)
            if saved:
                self.token = str(saved.get("token") or "").strip()
                self.base_url = str(saved.get("base_url") or self.base_url).strip() or ILINK_BASE_URL
                self.user_id = str(saved.get("user_id") or self.user_id).strip()

        self._context_tokens = ContextTokenStore(self.state_dir, self.account_id) if self.account_id else None
        self._processed_messages = ProcessedMessageStore(self.state_dir, self.account_id) if self.account_id else None

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.token)

    def get_profile(self) -> dict[str, Any]:
        return {
            "adapter_name": self.name,
            "surface_family": "weixin",
            "transport_mode": "polling",
            "supports_mentions": False,
            "supports_attachments": False,
            "supports_replies": True,
            "supports_updates": False,
            "supports_live_receive": False,
        }

    def assert_configured(self) -> None:
        if not self.configured:
            raise RuntimeError(
                "Weixin adapter is not configured. Run harborgate-weixin-login first, then set WEIXIN_ACCOUNT_ID."
            )

    def poll_updates(self, timeout_ms: int = DEFAULT_POLL_TIMEOUT_MS) -> list[dict[str, Any]]:
        self.assert_configured()
        sync_buf = load_sync_buf(self.state_dir, self.account_id)
        response = post_json(
            self.base_url,
            EP_GET_UPDATES,
            {"get_updates_buf": sync_buf},
            token=self.token,
            timeout_seconds=max(1, int(timeout_ms / 1000) + 10),
        )
        next_sync = str(response.get("get_updates_buf") or sync_buf)
        save_sync_buf(self.state_dir, self.account_id, next_sync)
        messages = response.get("msgs") or []
        return [item for item in messages if isinstance(item, dict)]

    def is_duplicate_update(self, payload: dict[str, Any]) -> bool:
        message_id = extract_weixin_message_id(payload)
        return bool(self._processed_messages and self._processed_messages.contains(message_id))

    def mark_update_processed(self, payload: dict[str, Any]) -> None:
        message_id = extract_weixin_message_id(payload)
        if self._processed_messages:
            self._processed_messages.add(message_id)

    def normalize_inbound(self, payload: dict[str, Any]) -> InboundMessage:
        sender_id = str(payload.get("from_user_id") or "").strip()
        room_id = str(payload.get("room_id") or "").strip()
        chat_id = room_id or sender_id
        text = extract_text_from_item_list(payload.get("item_list") or [])
        message_id = extract_weixin_message_id(payload)
        context_token = str(payload.get("context_token") or "").strip()
        route_key = str(payload.get("route_key") or "").strip()

        if room_id:
            raise ValueError("Weixin group chats are not supported yet in this starter")
        if not sender_id:
            raise ValueError("Weixin payload must include from_user_id")
        if not text:
            raise ValueError("Weixin payload does not contain a text message")

        if context_token and self._context_tokens:
            self._context_tokens.set(chat_id, context_token)

        return InboundMessage(
            platform="weixin",
            chat_id=chat_id,
            user_id=sender_id,
            text=text,
            message_id=message_id,
            chat_type="p2p",
            route_key=route_key,
            raw_payload=payload,
        )

    def send_outbound(self, outbound: OutboundMessage) -> dict[str, Any]:
        self.assert_configured()
        if not self._context_tokens:
            raise RuntimeError("Weixin context token store is unavailable")

        context_token = self._context_tokens.get(outbound.chat_id)
        if not context_token:
            raise RuntimeError(
                f"No Weixin context_token cached for chat_id={outbound.chat_id}. "
                "Send a DM from WeChat first so the gateway can learn the session token."
            )

        chunks = split_text_for_weixin(outbound.text)
        if not chunks:
            raise RuntimeError("Outbound Weixin message is empty")

        last_client_id = ""
        for chunk in chunks:
            payload = build_send_message_payload(
                to_user_id=outbound.chat_id,
                text=chunk,
                context_token=context_token,
            )
            last_client_id = str((payload.get("msg") or {}).get("client_id") or "")
            post_json(self.base_url, EP_SEND_MESSAGE, payload, token=self.token)

        return {
            "platform": "weixin",
            "chat_id": outbound.chat_id,
            "text": outbound.text,
            "timestamp": outbound.timestamp,
            "delivery": "weixin",
            "sent": True,
            "message_id": last_client_id,
            "metadata": {
                **outbound.metadata,
                "context_token_used": True,
                "chunk_count": len(chunks),
            },
        }

from __future__ import annotations

import base64
import hashlib
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
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_FILE = 4
ITEM_VIDEO = 5
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
UPLOAD_MEDIA_IMAGE = 1
UPLOAD_MEDIA_VIDEO = 2
UPLOAD_MEDIA_FILE = 3
WEIXIN_MEDIA_ENCRYPT_TYPE = 1
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_POLL_TIMEOUT_MS = 35_000
MAX_TEXT_CHUNK_LENGTH = 900
CDN_UPLOAD_MAX_RETRIES = 3


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(value: str) -> str:
    text = (value or "").strip() or "default"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)


def _redact_sensitive_text(value: str, *secrets: str) -> str:
    redacted = str(value or "")
    for secret in secrets:
        secret_value = str(secret or "").strip()
        if secret_value:
            redacted = redacted.replace(secret_value, "[REDACTED]")
    redacted = redacted.replace("Bearer ", "Bearer [REDACTED] ")
    return redacted


def is_weixin_dns_resolution_error(error_text: str) -> bool:
    normalized = str(error_text or "").strip().lower()
    return any(
        marker in normalized
        for marker in (
            "getaddrinfo",
            "name resolution",
            "temporary failure in name resolution",
            "name or service not known",
            "nodename nor servname",
            "nameresolutionerror",
            "socket.gaierror",
        )
    )


def is_weixin_provider_auth_error(error_text: str) -> bool:
    normalized = str(error_text or "").strip().lower()
    return any(marker in normalized for marker in ("401", "403", "auth", "token", "forbidden"))


def _poll_status_for_error(error_text: str) -> str:
    normalized = error_text.lower()
    if "read operation timed out" in normalized:
        return "idle_timeout"
    if "timed out" in normalized or "timeout" in normalized:
        return "timeout"
    return "error"


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


def _transport_state_file(state_dir: str | Path, account_id: str) -> Path:
    return _account_dir(state_dir) / f"{_safe_slug(account_id)}.runtime.json"


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


def load_weixin_transport_state(state_dir: str | Path, account_id: str) -> dict[str, Any] | None:
    path = _transport_state_file(state_dir, account_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def save_weixin_transport_state(state_dir: str | Path, account_id: str, payload: dict[str, Any]) -> None:
    path = _transport_state_file(state_dir, account_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_weixin_context_tokens(state_dir: str | Path, account_id: str) -> dict[str, str]:
    path = _context_file(state_dir, account_id)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in payload.items()
        if str(key).strip() and str(value).strip()
    }


def save_weixin_context_tokens(state_dir: str | Path, account_id: str, payload: dict[str, Any]) -> None:
    path = _context_file(state_dir, account_id)
    sanitized = {
        str(key): str(value)
        for key, value in payload.items()
        if str(key).strip() and str(value).strip()
    }
    path.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8")


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
        self._cache = load_weixin_context_tokens(self.path.parent.parent, self.path.stem.removesuffix(".context_tokens"))

    def _persist(self) -> None:
        save_weixin_context_tokens(self.path.parent.parent, self.path.stem.removesuffix(".context_tokens"), self._cache)


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
    items = [{"type": ITEM_TEXT, "text_item": {"text": text}}] if text else []
    return build_send_message_payload_items(
        to_user_id=to_user_id,
        item_list=items,
        context_token=context_token,
        client_id=client_id,
    )


def build_send_message_payload_items(
    *,
    to_user_id: str,
    item_list: list[dict[str, Any]],
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
        }
    }
    if item_list:
        payload["msg"]["item_list"] = item_list
    if context_token:
        payload["msg"]["context_token"] = context_token
    return payload


def _md5_hex(payload: bytes) -> str:
    return hashlib.md5(payload).hexdigest()


def _aes_ecb_padded_size(plaintext_size: int) -> int:
    return ((plaintext_size // 16) + 1) * 16


def _encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - runtime dependency boundary
        raise RuntimeError(
            "Weixin native image send requires the cryptography package for AES-128-ECB"
        ) from exc

    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _build_cdn_upload_url(*, cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={parse.quote(upload_param)}"
        f"&filekey={parse.quote(filekey)}"
    )


def _upload_binary_to_cdn(
    *,
    plaintext: bytes,
    upload_full_url: str | None,
    upload_param: str | None,
    filekey: str,
    cdn_base_url: str,
    aeskey: bytes,
    label: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    ciphertext = _encrypt_aes_ecb(plaintext, aeskey)
    trimmed_full_url = str(upload_full_url or "").strip()
    if trimmed_full_url:
        upload_url = trimmed_full_url
    else:
        trimmed_upload_param = str(upload_param or "").strip()
        if not trimmed_upload_param:
            raise RuntimeError(f"{label}: CDN upload URL missing")
        upload_url = _build_cdn_upload_url(
            cdn_base_url=cdn_base_url,
            upload_param=trimmed_upload_param,
            filekey=filekey,
        )

    last_error: Exception | None = None
    for attempt in range(1, CDN_UPLOAD_MAX_RETRIES + 1):
        req = request.Request(
            upload_url,
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                encrypted_param = response.headers.get("x-encrypted-param", "").strip()
                if not encrypted_param:
                    raise RuntimeError("CDN upload response missing x-encrypted-param header")
                return encrypted_param
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            status_message = exc.headers.get("x-error-message", "").strip() or detail or str(exc)
            if 400 <= exc.code < 500:
                raise RuntimeError(
                    f"{label}: CDN client error {exc.code}: {status_message}"
                ) from exc
            last_error = RuntimeError(f"{label}: CDN server error {exc.code}: {status_message}")
        except Exception as exc:  # pragma: no cover - network retry boundary
            last_error = exc if isinstance(exc, RuntimeError) else RuntimeError(str(exc))
        if attempt >= CDN_UPLOAD_MAX_RETRIES:
            break
    raise RuntimeError(str(last_error or f"{label}: CDN upload failed"))


def _resolve_local_attachment_path(raw_path: str) -> Path | None:
    normalized = str(raw_path or "").strip()
    if not normalized:
        return None

    candidate = Path(normalized)
    if candidate.is_file():
        return candidate

    if candidate.is_absolute():
        return None

    roots = []
    for env_name in (
        "HARBOR_CAPTURE_ROOT",
        "HARBOR_HARBOROS_WRITABLE_ROOT",
        "HARBOR_RELEASE_INSTALL_ROOT",
        "WORKSPACE_ROOT",
    ):
        root = str(os.getenv(env_name, "")).strip()
        if root:
            roots.append(Path(root))
    roots.append(Path.cwd())

    for root in roots:
        resolved = root / normalized
        if resolved.is_file():
            return resolved
    return None


@dataclass(slots=True)
class WeixinUploadedImage:
    filekey: str
    original_download_param: str
    aeskey_hex: str
    original_size: int
    original_ciphertext_size: int
    thumbnail_download_param: str = ""
    thumbnail_size: int = 0
    thumbnail_ciphertext_size: int = 0


@dataclass(slots=True)
class WeixinUploadedMedia:
    filekey: str
    download_param: str
    aeskey_hex: str
    plaintext_size: int
    ciphertext_size: int


@dataclass(slots=True)
class NativeWeixinAttachment:
    delivery_kind: str
    path: Path
    mime_type: str
    file_name: str


def _upload_image_artifact_to_weixin(
    *,
    image_path: Path,
    to_user_id: str,
    base_url: str,
    token: str,
    cdn_base_url: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> WeixinUploadedImage:
    original_bytes = image_path.read_bytes()
    if not original_bytes:
        raise RuntimeError(f"Weixin image artifact is empty: {image_path}")

    filekey = os.urandom(16).hex()
    aeskey = os.urandom(16)
    upload_payload = {
        "filekey": filekey,
        "media_type": UPLOAD_MEDIA_IMAGE,
        "to_user_id": to_user_id,
        "rawsize": len(original_bytes),
        "rawfilemd5": _md5_hex(original_bytes),
        "filesize": _aes_ecb_padded_size(len(original_bytes)),
        "no_need_thumb": True,
        "aeskey": aeskey.hex(),
    }
    upload_response = post_json(
        base_url,
        EP_GET_UPLOAD_URL,
        upload_payload,
        token=token,
        timeout_seconds=timeout_seconds,
    )
    upload_full_url = str(upload_response.get("upload_full_url") or "").strip() or None
    upload_param = str(upload_response.get("upload_param") or "").strip() or None
    thumb_upload_param = str(upload_response.get("thumb_upload_param") or "").strip() or None
    if not upload_full_url and not upload_param:
        raise RuntimeError("Weixin getuploadurl returned no upload URL for the original image")

    original_download_param = _upload_binary_to_cdn(
        plaintext=original_bytes,
        upload_full_url=upload_full_url,
        upload_param=upload_param,
        filekey=filekey,
        cdn_base_url=cdn_base_url,
        aeskey=aeskey,
        label="weixin-image-orig",
        timeout_seconds=timeout_seconds,
    )
    return WeixinUploadedImage(
        filekey=filekey,
        original_download_param=original_download_param,
        aeskey_hex=aeskey.hex(),
        original_size=len(original_bytes),
        original_ciphertext_size=_aes_ecb_padded_size(len(original_bytes)),
    )


def _build_native_image_message_item(uploaded: WeixinUploadedImage) -> dict[str, Any]:
    aes_key_base64 = base64.b64encode(uploaded.aeskey_hex.encode("ascii")).decode("ascii")
    image_item: dict[str, Any] = {
        "media": {
            "encrypt_query_param": uploaded.original_download_param,
            "aes_key": aes_key_base64,
            "encrypt_type": WEIXIN_MEDIA_ENCRYPT_TYPE,
        },
        "mid_size": uploaded.original_ciphertext_size,
    }
    return {"type": ITEM_IMAGE, "image_item": image_item}


def _build_cdn_media_reference(download_param: str, aeskey_hex: str) -> dict[str, Any]:
    aes_key_base64 = base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii")
    return {
        "encrypt_query_param": download_param,
        "aes_key": aes_key_base64,
        "encrypt_type": WEIXIN_MEDIA_ENCRYPT_TYPE,
    }


def _upload_media_artifact_to_weixin(
    *,
    file_path: Path,
    media_type: int,
    to_user_id: str,
    base_url: str,
    token: str,
    cdn_base_url: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> WeixinUploadedMedia:
    file_bytes = file_path.read_bytes()
    if not file_bytes:
        raise RuntimeError(f"Weixin media artifact is empty: {file_path}")

    filekey = os.urandom(16).hex()
    aeskey = os.urandom(16)
    upload_payload = {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": to_user_id,
        "rawsize": len(file_bytes),
        "rawfilemd5": _md5_hex(file_bytes),
        "filesize": _aes_ecb_padded_size(len(file_bytes)),
        "no_need_thumb": True,
        "aeskey": aeskey.hex(),
    }
    upload_response = post_json(
        base_url,
        EP_GET_UPLOAD_URL,
        upload_payload,
        token=token,
        timeout_seconds=timeout_seconds,
    )
    upload_full_url = str(upload_response.get("upload_full_url") or "").strip() or None
    upload_param = str(upload_response.get("upload_param") or "").strip() or None
    if not upload_full_url and not upload_param:
        raise RuntimeError("Weixin getuploadurl returned no upload URL for the media artifact")

    download_param = _upload_binary_to_cdn(
        plaintext=file_bytes,
        upload_full_url=upload_full_url,
        upload_param=upload_param,
        filekey=filekey,
        cdn_base_url=cdn_base_url,
        aeskey=aeskey,
        label="weixin-media-orig",
        timeout_seconds=timeout_seconds,
    )
    return WeixinUploadedMedia(
        filekey=filekey,
        download_param=download_param,
        aeskey_hex=aeskey.hex(),
        plaintext_size=len(file_bytes),
        ciphertext_size=_aes_ecb_padded_size(len(file_bytes)),
    )


def _build_native_video_message_item(uploaded: WeixinUploadedMedia) -> dict[str, Any]:
    return {
        "type": ITEM_VIDEO,
        "video_item": {
            "media": _build_cdn_media_reference(uploaded.download_param, uploaded.aeskey_hex),
            "video_size": uploaded.ciphertext_size,
        },
    }


def _build_native_file_message_item(
    uploaded: WeixinUploadedMedia,
    *,
    file_name: str,
) -> dict[str, Any]:
    return {
        "type": ITEM_FILE,
        "file_item": {
            "media": _build_cdn_media_reference(uploaded.download_param, uploaded.aeskey_hex),
            "file_name": file_name,
            "len": str(uploaded.plaintext_size),
        },
    }


def _build_text_message_item(text: str) -> dict[str, Any]:
    return {"type": ITEM_TEXT, "text_item": {"text": text}}


def _should_send_native_attachment_reply(outbound: OutboundMessage) -> bool:
    return str(outbound.metadata.get("source") or "").strip() == "harborbeacon" and bool(outbound.attachments)


def _resolve_native_media_attachment(outbound: OutboundMessage) -> NativeWeixinAttachment:
    attachments = [item for item in outbound.attachments if isinstance(item, dict)]
    if len(attachments) != 1:
        raise RuntimeError("Weixin native media reply requires exactly one attachment")

    attachment = attachments[0]
    kind = str(attachment.get("kind") or attachment.get("type") or "").strip().lower()
    mime_type = str(attachment.get("mime_type") or "").strip().lower()
    path = _resolve_local_attachment_path(str(attachment.get("path") or ""))
    if path is None:
        raise RuntimeError("Weixin native media reply requires a readable same-host attachment path")

    delivery_kind = "file"
    if kind == "image" or mime_type.startswith("image/"):
        delivery_kind = "image"
    elif kind == "video" or mime_type.startswith("video/"):
        delivery_kind = "video"

    metadata = dict(attachment.get("metadata") or {}) if isinstance(attachment.get("metadata"), dict) else {}
    file_name = (
        str(metadata.get("file_name") or "").strip()
        or str(attachment.get("label") or "").strip()
        or path.name
    )
    return NativeWeixinAttachment(
        delivery_kind=delivery_kind,
        path=path,
        mime_type=mime_type,
        file_name=file_name,
    )


def _send_message_items(
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    context_token: str | None,
    item_list: list[dict[str, Any]],
) -> str:
    payload = build_send_message_payload_items(
        to_user_id=to_user_id,
        item_list=item_list,
        context_token=context_token,
    )
    client_id = str((payload.get("msg") or {}).get("client_id") or "")
    post_json(base_url, EP_SEND_MESSAGE, payload, token=token)
    return client_id


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
        self.cdn_base_url = str(os.getenv("WEIXIN_CDN_BASE_URL", DEFAULT_CDN_BASE_URL)).strip() or DEFAULT_CDN_BASE_URL
        self.user_id = os.getenv("WEIXIN_USER_ID", "").strip()
        self._transport_state: dict[str, Any] = {
            "mode": "polling",
            "status": "waiting_for_credentials" if not self.configured else "polling_idle",
            "connected": False,
            "last_error": "",
            "last_poll_outcome": "never_polled" if self.configured else "waiting_for_credentials",
            "last_poll_at": "",
            "last_getupdates_at": "",
            "last_getupdates_buf": "",
            "last_getupdates_count": 0,
            "last_private_text_message_count": 0,
            "last_private_text_message_at": "",
            "last_getupdates_message_ids": [],
            "last_getupdates_private_message_ids": [],
            "last_getupdates_error": "",
            "last_context_token_at": "",
            "last_send_at": "",
            "last_send_chunk_count": 0,
            "last_send_status": "",
            "last_send_error": "",
            "last_send_retryable": False,
            "last_send_provider_message_id": "",
            "last_send_context_token_used": False,
            "last_send_attachment_count": 0,
            "last_send_content_kind": "",
            "last_inbound_at": "",
            "last_inbound_message_id": "",
            "last_inbound_chat_id": "",
        }

        if self.account_id and not self.token:
            saved = load_weixin_account(self.state_dir, self.account_id)
            if saved:
                self.token = str(saved.get("token") or "").strip()
                self.base_url = str(saved.get("base_url") or self.base_url).strip() or ILINK_BASE_URL
                self.user_id = str(saved.get("user_id") or self.user_id).strip()

        self._context_tokens = ContextTokenStore(self.state_dir, self.account_id) if self.account_id else None
        self._processed_messages = ProcessedMessageStore(self.state_dir, self.account_id) if self.account_id else None
        if self.account_id:
            persisted_transport = load_weixin_transport_state(self.state_dir, self.account_id)
            if isinstance(persisted_transport, dict):
                self._transport_state.update(persisted_transport)
                self._transport_state["mode"] = "polling"

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.token)

    def get_profile(self) -> dict[str, Any]:
        return {
            "adapter_name": self.name,
            "surface_family": "weixin",
            "transport_mode": "polling",
            "supports_mentions": False,
            "supports_attachments": True,
            "supports_replies": True,
            "supports_updates": False,
            "supports_live_receive": False,
        }

    def transport_status(self) -> dict[str, Any]:
        state = dict(self._transport_state)
        state["last_error"] = _redact_sensitive_text(state.get("last_error", ""), self.account_id, self.token)
        state["last_getupdates_error"] = _redact_sensitive_text(
            state.get("last_getupdates_error", ""),
            self.account_id,
            self.token,
        )
        state["last_send_error"] = _redact_sensitive_text(
            state.get("last_send_error", ""),
            self.account_id,
            self.token,
        )
        return state

    def assert_configured(self) -> None:
        if not self.configured:
            raise RuntimeError(
                "Weixin adapter is not configured. Run harborgate-weixin-login first, then set WEIXIN_ACCOUNT_ID."
            )

    def poll_updates(self, timeout_ms: int = DEFAULT_POLL_TIMEOUT_MS) -> list[dict[str, Any]]:
        self.assert_configured()
        self._set_transport_state(
            status="polling",
            connected=False,
            last_error="",
            last_poll_outcome="polling",
            last_poll_at=utc_now_iso(),
            last_getupdates_error="",
            last_getupdates_message_ids=[],
            last_getupdates_private_message_ids=[],
        )
        sync_buf = load_sync_buf(self.state_dir, self.account_id)
        try:
            response = post_json(
                self.base_url,
                EP_GET_UPDATES,
                {"get_updates_buf": sync_buf},
                token=self.token,
                timeout_seconds=max(1, int(timeout_ms / 1000) + 10),
            )
        except Exception as exc:
            error_text = _redact_sensitive_text(str(exc), self.account_id, self.token)
            poll_status = _poll_status_for_error(error_text)
            observed_at = utc_now_iso()
            if poll_status == "idle_timeout":
                self._set_transport_state(
                    status="polling_idle",
                    connected=True,
                    last_error="",
                    last_poll_outcome="idle_timeout",
                    last_poll_at=observed_at,
                    last_getupdates_at=observed_at,
                    last_getupdates_buf=sync_buf,
                    last_getupdates_error="",
                    last_getupdates_count=0,
                    last_private_text_message_count=0,
                    last_getupdates_message_ids=[],
                    last_getupdates_private_message_ids=[],
                )
                return []
            self._set_transport_state(
                status=poll_status,
                connected=False,
                last_error=error_text,
                last_poll_outcome="error",
                last_poll_at=observed_at,
                last_getupdates_at=observed_at,
                last_getupdates_error=error_text,
                last_getupdates_count=0,
                last_private_text_message_count=0,
                last_getupdates_message_ids=[],
                last_getupdates_private_message_ids=[],
            )
            raise
        next_sync = str(response.get("get_updates_buf") or sync_buf)
        save_sync_buf(self.state_dir, self.account_id, next_sync)
        messages = response.get("msgs") or []
        private_messages = [
            item
            for item in messages
            if isinstance(item, dict) and not str(item.get("room_id") or "").strip()
        ]
        message_ids = [
            extract_weixin_message_id(item)
            for item in messages
            if isinstance(item, dict) and extract_weixin_message_id(item)
        ]
        private_message_ids = [
            extract_weixin_message_id(item)
            for item in private_messages
            if isinstance(item, dict) and extract_weixin_message_id(item)
        ]
        self._set_transport_state(
            status="polling_idle",
            connected=True,
            last_error="",
            last_poll_outcome="messages" if messages else "empty",
            last_getupdates_at=utc_now_iso(),
            last_getupdates_buf=next_sync,
            last_getupdates_count=len([item for item in messages if isinstance(item, dict)]),
            last_private_text_message_count=len(private_messages),
            last_getupdates_message_ids=message_ids,
            last_getupdates_private_message_ids=private_message_ids,
            last_getupdates_error="",
            **({"last_private_text_message_at": utc_now_iso()} if private_messages else {}),
        )
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

        observed_at = utc_now_iso()
        self._set_transport_state(
            connected=True,
            last_inbound_at=observed_at,
            last_inbound_message_id=message_id,
            last_inbound_chat_id=chat_id,
            last_private_text_message_at=observed_at,
        )
        if context_token and self._context_tokens:
            self._context_tokens.set(chat_id, context_token)
            self._set_transport_state(
                last_context_token_at=observed_at,
            )

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

        native_attachment = (
            _resolve_native_media_attachment(outbound)
            if _should_send_native_attachment_reply(outbound)
            else None
        )
        chunks = split_text_for_weixin(outbound.text) if native_attachment is None else []
        if native_attachment is None and not chunks:
            raise RuntimeError("Outbound Weixin message is empty")
        native_caption = outbound.text.strip()
        send_unit_count = len(chunks)
        if native_attachment is not None:
            send_unit_count = 1 + (1 if native_caption else 0)
        delivered_attachment_kind = native_attachment.delivery_kind if native_attachment is not None else ""
        attachment_fallback_used = False

        observed_at = utc_now_iso()
        self._set_transport_state(
            status="sending",
            connected=True,
            last_error="",
            last_send_at=observed_at,
            last_send_chunk_count=send_unit_count,
            last_inbound_chat_id=outbound.chat_id,
            last_send_status="sending",
            last_send_error="",
            last_send_retryable=False,
            last_send_provider_message_id="",
            last_send_context_token_used=True,
            last_send_attachment_count=len(outbound.attachments),
            last_send_content_kind=(
                f"text+{native_attachment.delivery_kind}" if native_attachment is not None else "text"
            ),
        )
        last_client_id = ""
        try:
            if native_attachment is not None and native_attachment.delivery_kind == "image":
                uploaded_image = _upload_image_artifact_to_weixin(
                    image_path=native_attachment.path,
                    to_user_id=outbound.chat_id,
                    base_url=self.base_url,
                    token=self.token,
                    cdn_base_url=self.cdn_base_url,
                )
                if native_caption:
                    last_client_id = _send_message_items(
                        base_url=self.base_url,
                        token=self.token,
                        to_user_id=outbound.chat_id,
                        context_token=context_token,
                        item_list=[_build_text_message_item(native_caption)],
                    )
                last_client_id = _send_message_items(
                    base_url=self.base_url,
                    token=self.token,
                    to_user_id=outbound.chat_id,
                    context_token=context_token,
                    item_list=[_build_native_image_message_item(uploaded_image)],
                )
            elif native_attachment is not None and native_attachment.delivery_kind == "video":
                try:
                    uploaded_video = _upload_media_artifact_to_weixin(
                        file_path=native_attachment.path,
                        media_type=UPLOAD_MEDIA_VIDEO,
                        to_user_id=outbound.chat_id,
                        base_url=self.base_url,
                        token=self.token,
                        cdn_base_url=self.cdn_base_url,
                    )
                    if native_caption:
                        last_client_id = _send_message_items(
                            base_url=self.base_url,
                            token=self.token,
                            to_user_id=outbound.chat_id,
                            context_token=context_token,
                            item_list=[_build_text_message_item(native_caption)],
                        )
                    last_client_id = _send_message_items(
                        base_url=self.base_url,
                        token=self.token,
                        to_user_id=outbound.chat_id,
                        context_token=context_token,
                        item_list=[_build_native_video_message_item(uploaded_video)],
                    )
                except Exception as video_exc:
                    attachment_fallback_used = True
                    delivered_attachment_kind = "file"
                    uploaded_file = _upload_media_artifact_to_weixin(
                        file_path=native_attachment.path,
                        media_type=UPLOAD_MEDIA_FILE,
                        to_user_id=outbound.chat_id,
                        base_url=self.base_url,
                        token=self.token,
                        cdn_base_url=self.cdn_base_url,
                    )
                    fallback_caption = (
                        f"{native_caption}（以文件发送）" if native_caption else "完整回放如下（以文件发送）"
                    )
                    try:
                        last_client_id = _send_message_items(
                            base_url=self.base_url,
                            token=self.token,
                            to_user_id=outbound.chat_id,
                            context_token=context_token,
                            item_list=[_build_text_message_item(fallback_caption)],
                        )
                        last_client_id = _send_message_items(
                            base_url=self.base_url,
                            token=self.token,
                            to_user_id=outbound.chat_id,
                            context_token=context_token,
                            item_list=[
                                _build_native_file_message_item(
                                    uploaded_file,
                                    file_name=native_attachment.file_name,
                                )
                            ],
                        )
                    except Exception as file_exc:
                        raise RuntimeError(
                            f"native video send failed: {video_exc}; fallback file send failed: {file_exc}"
                        ) from file_exc
            elif native_attachment is not None:
                if native_caption:
                    last_client_id = _send_message_items(
                        base_url=self.base_url,
                        token=self.token,
                        to_user_id=outbound.chat_id,
                        context_token=context_token,
                        item_list=[_build_text_message_item(native_caption)],
                    )
                uploaded_file = _upload_media_artifact_to_weixin(
                    file_path=native_attachment.path,
                    media_type=UPLOAD_MEDIA_FILE,
                    to_user_id=outbound.chat_id,
                    base_url=self.base_url,
                    token=self.token,
                    cdn_base_url=self.cdn_base_url,
                )
                last_client_id = _send_message_items(
                    base_url=self.base_url,
                    token=self.token,
                    to_user_id=outbound.chat_id,
                    context_token=context_token,
                    item_list=[
                        _build_native_file_message_item(
                            uploaded_file,
                            file_name=native_attachment.file_name,
                        )
                    ],
                )
            else:
                for chunk in chunks:
                    payload = build_send_message_payload(
                        to_user_id=outbound.chat_id,
                        text=chunk,
                        context_token=context_token,
                    )
                    last_client_id = str((payload.get("msg") or {}).get("client_id") or "")
                    post_json(self.base_url, EP_SEND_MESSAGE, payload, token=self.token)
        except Exception as exc:
            error_text = _redact_sensitive_text(str(exc), self.account_id, self.token, context_token)
            self._set_transport_state(
                status="send_failed",
                connected=True,
                last_send_at=utc_now_iso(),
                last_error=error_text,
                last_send_status="failed",
                last_send_error=error_text,
                last_send_retryable=False if native_attachment is not None else True,
                last_send_provider_message_id=last_client_id,
                last_send_context_token_used=True,
                last_send_content_kind=(
                    f"text+{delivered_attachment_kind}" if native_attachment is not None else "text"
                ),
            )
            raise

        self._set_transport_state(
            status="polling_idle",
            connected=True,
            last_send_at=utc_now_iso(),
            last_error="",
            last_send_status="sent",
            last_send_error="",
            last_send_retryable=False,
            last_send_provider_message_id=last_client_id,
            last_send_context_token_used=True,
            last_send_attachment_count=len(outbound.attachments),
            last_send_content_kind=(
                f"text+{delivered_attachment_kind}" if native_attachment is not None else "text"
            ),
        )
        return {
            "platform": "weixin",
            "chat_id": outbound.chat_id,
            "text": outbound.text,
            "timestamp": outbound.timestamp,
            "delivery": "weixin",
            "sent": True,
            "message_id": last_client_id,
            "provider_message_id": last_client_id,
            "attachments": [dict(item) for item in outbound.attachments if isinstance(item, dict)],
            "metadata": {
                **outbound.metadata,
                "context_token_used": True,
                "chunk_count": send_unit_count,
                "attachment_count": len(outbound.attachments),
                "native_image_reply": native_attachment is not None and delivered_attachment_kind == "image",
                "native_attachment_kind": delivered_attachment_kind,
                "native_attachment_fallback": attachment_fallback_used,
            },
        }

    def _set_transport_state(self, **updates: Any) -> None:
        self._transport_state.update(updates)
        self._transport_state["mode"] = "polling"
        if self.account_id:
            save_weixin_transport_state(self.state_dir, self.account_id, self._transport_state)

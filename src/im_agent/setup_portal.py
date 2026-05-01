from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib import parse

try:
    from qrcodegen import QrCode
except ImportError:  # pragma: no cover - optional runtime dependency fallback
    QrCode = None  # type: ignore[assignment]

from im_agent import __version__
from im_agent.gateway import GatewayService
from im_agent.platforms.feishu import FeishuAdapter, FeishuSettings
from im_agent.platforms.weixin import (
    EP_GET_QR_STATUS,
    ILINK_BASE_URL,
    WeixinAdapter,
    clear_weixin_account_state,
    get_json,
    is_weixin_dns_resolution_error,
    is_weixin_provider_auth_error,
    load_weixin_account,
    load_weixin_context_tokens,
    load_weixin_transport_state,
    request_weixin_qr_challenge,
    save_weixin_account,
)


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _generate_session_code() -> str:
    token = secrets.token_hex(4).upper()
    return f"{token[:4]}-{token[4:]}"


def _mask_secret(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:4]}***{text[-2:]}"


_SENSITIVE_STATUS_KEYS = {
    "api_key",
    "app_secret",
    "access_token",
    "tenant_access_token",
    "bot_token",
    "context_token",
    "verification_token",
    "encrypt_key",
    "authorization",
    "token",
    "secret",
}


def _redact_known_secrets(value: str, *secrets: str) -> str:
    redacted = str(value or "")
    for secret in secrets:
        normalized_secret = str(secret or "").strip()
        if normalized_secret:
            redacted = redacted.replace(normalized_secret, "[REDACTED]")
    redacted = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", redacted)

    def _replace_assignment(match: re.Match[str]) -> str:
        return f"{match.group(1)}=[REDACTED]"

    redacted = re.sub(
        r"(?i)\b(api_key|app_secret|access_token|tenant_access_token|bot_token|context_token|authorization|token|secret)\s*[:=]\s*([^\s,;]+)",
        _replace_assignment,
        redacted,
    )
    return redacted


def _redact_status_payload(value: Any, *secrets: str) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key).strip().lower()
            if key_text in _SENSITIVE_STATUS_KEYS:
                redacted[key] = "[REDACTED]" if str(item or "").strip() else ""
                continue
            redacted[key] = _redact_status_payload(item, *secrets)
        return redacted
    if isinstance(value, list):
        return [_redact_status_payload(item, *secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_status_payload(item, *secrets) for item in value)
    if isinstance(value, str):
        return _redact_known_secrets(value, *secrets)
    return value


def _html_escape(value: str) -> str:
    escaped = (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
    return escaped


def _classify_weixin_transport_blocker(
    *,
    configured: bool,
    connected: bool,
    poll_status: str,
    poll_outcome: str,
    poll_error: str,
    private_text_message_count: int,
    private_text_confirmed: bool,
    context_token_count: int,
    last_send_status: str,
    last_send_error: str,
) -> str:
    if not configured:
        return "account_restore"
    if poll_status == "timeout":
        return "weixin_poll_timeout"
    if poll_status == "error" or (not connected and poll_error):
        if is_weixin_dns_resolution_error(poll_error):
            return "weixin_dns_resolution"
        if is_weixin_provider_auth_error(poll_error):
            return "weixin_provider_auth_failed"
        return "weixin_poll_error"
    if private_text_message_count <= 0 and not private_text_confirmed:
        if poll_outcome in {"idle_timeout", "empty"} or poll_status in {"polling", "polling_idle"}:
            return "weixin_waiting_for_private_text"
        return "weixin_live_ingress_not_confirmed"
    if context_token_count <= 0:
        return "context_token_send"
    if last_send_status in {"failed", "send_failed", "error"}:
        if is_weixin_dns_resolution_error(last_send_error):
            return "weixin_dns_resolution"
        return "weixin_live_send_failed"
    return ""


def _classify_weixin_ingress_blocker(
    *,
    configured: bool,
    connected: bool,
    poll_status: str,
    poll_error: str,
    private_text_confirmed: bool,
    context_token_count: int,
    last_send_status: str,
) -> str:
    if not configured:
        return "account_restore"
    if poll_status in {"error", "timeout"}:
        if is_weixin_provider_auth_error(poll_error):
            return "qr_recovery"
        return "getupdates"
    if not connected or not private_text_confirmed:
        return "getupdates"
    if context_token_count <= 0 or last_send_status in {"failed", "send_failed", "error"}:
        return "context_token_send"
    return ""


def _qr_to_svg(text: str, border: int = 4) -> str:
    if QrCode is None:
        safe_text = _html_escape(text)
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180">'
            '<rect width="100%" height="100%" fill="#f8f3ec"/>'
            '<text x="20" y="48" font-size="18" fill="#1b1814">QR dependency missing</text>'
            f'<text x="20" y="88" font-size="12" fill="#5b534a">{safe_text}</text>'
            "</svg>"
        )

    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    size = qr.get_size()
    dimension = size + border * 2
    path_parts: list[str] = []
    for y in range(size):
        for x in range(size):
            if qr.get_module(x, y):
                path_parts.append(f"M{x + border},{y + border}h1v1h-1z")
    path = " ".join(path_parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dimension} {dimension}" '
        'shape-rendering="crispEdges">'
        '<rect width="100%" height="100%" fill="#f8f3ec"/>'
        f'<path d="{path}" fill="#1b1814"/>'
        "</svg>"
    )


class FileSetupPortalStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "_setup_portal.json"
        self._lock = threading.Lock()

    def load_state(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                payload = self._bootstrap_state({})
                self._write_state(payload)
                return payload
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                payload = {}
            payload = self._bootstrap_state(payload)
            self._write_state(payload)
            return payload

    def save_feishu_state(self, next_feishu: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                state = self._bootstrap_state({})
            else:
                with self.path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                state = self._bootstrap_state(payload if isinstance(payload, dict) else {})
            state["feishu"] = dict(next_feishu)
            state["updated_at"] = _now_utc()
            self._write_state(state)
            return dict(state["feishu"])

    def load_weixin_login_state(self) -> dict[str, Any]:
        state = self.load_state()
        login = state.get("weixin_login")
        return dict(login) if isinstance(login, dict) else {}

    def save_weixin_login_state(self, next_login: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                state = self._bootstrap_state({})
            else:
                with self.path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                state = self._bootstrap_state(payload if isinstance(payload, dict) else {})
            state["weixin_login"] = dict(next_login)
            state["updated_at"] = _now_utc()
            self._write_state(state)
            return dict(state["weixin_login"])

    def current_session_code(self) -> str:
        state = self.load_state()
        return str(state.get("session_code") or "")

    def _bootstrap_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = dict(payload)
        session_code = str(state.get("session_code") or "").strip().upper()
        if not session_code:
            session_code = _generate_session_code()
        state["session_code"] = session_code
        feishu = state.get("feishu")
        state["feishu"] = dict(feishu) if isinstance(feishu, dict) else {}
        weixin_login = state.get("weixin_login")
        state["weixin_login"] = dict(weixin_login) if isinstance(weixin_login, dict) else {}
        state.setdefault("updated_at", "")
        return state

    def _write_state(self, payload: dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)


class SetupPortalService:
    def __init__(
        self,
        *,
        gateway: GatewayService,
        store: FileSetupPortalStore,
        bind_host: str,
        bind_port: int,
        public_origin: str = "",
        weixin_state_dir: str | Path | None = None,
        runtime_root: str | Path | None = None,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.public_origin = public_origin.strip().rstrip("/")
        self.weixin_state_dir = Path(weixin_state_dir or Path(store.root) / "weixin")
        self.runtime_root = Path(runtime_root or Path(store.root) / "runtime")

    def bootstrap(self) -> None:
        feishu_state = self.store.load_state().get("feishu") or {}
        if not isinstance(feishu_state, dict):
            return
        app_id = str(feishu_state.get("app_id") or "").strip()
        app_secret = str(feishu_state.get("app_secret") or "").strip()
        if not (app_id and app_secret):
            return

        adapter = self.ensure_feishu_adapter()
        settings = self._build_feishu_settings(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=str(feishu_state.get("verification_token") or "").strip(),
            connection_mode=str(feishu_state.get("connection_mode") or "websocket").strip() or "websocket",
            enable_live_send=bool(feishu_state.get("enable_live_send", True)),
            app_name=str(feishu_state.get("app_name") or "").strip(),
            bot_open_id=str(feishu_state.get("bot_open_id") or "").strip(),
            bot_user_id=str(feishu_state.get("bot_user_id") or "").strip(),
        )
        adapter.apply_settings(settings)
        self._bootstrap_weixin_adapter()

    def _bootstrap_weixin_adapter(self) -> None:
        record = self._discover_weixin_account_state()
        if not record:
            return
        account_id = str(record.get("account_id") or "").strip()
        token = str(record.get("token") or "").strip()
        if not (account_id and token):
            return
        base_url = str(record.get("base_url") or "").strip() or None
        existing = self.gateway.get_adapter("weixin")
        if isinstance(existing, WeixinAdapter) and existing.configured:
            return
        self.gateway.register_adapter(
            WeixinAdapter(
                state_dir=self.weixin_state_dir,
                account_id=account_id,
                token=token,
                base_url=base_url,
            )
        )

    def _discover_weixin_account_state(self) -> dict[str, Any]:
        env_account_id = os.getenv("WEIXIN_ACCOUNT_ID", "").strip()
        if env_account_id:
            record = load_weixin_account(self.weixin_state_dir, env_account_id)
            if isinstance(record, dict):
                return record

        accounts_dir = Path(self.weixin_state_dir) / "accounts"
        if not accounts_dir.exists():
            return {}

        ignored_suffixes = (".sync.json", ".context_tokens.json", ".processed_messages.json", ".runtime.json")
        for path in sorted(accounts_dir.glob("*.json")):
            if any(path.name.endswith(suffix) for suffix in ignored_suffixes):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(payload, dict):
                return payload
        return {}

    def unbind_weixin_account(self) -> dict[str, Any]:
        record = self._discover_weixin_account_state()
        adapter = self.gateway.get_adapter("weixin")
        account_id = str(record.get("account_id") or getattr(adapter, "account_id", "") or "").strip()
        deleted = clear_weixin_account_state(self.weixin_state_dir, account_id)

        if isinstance(adapter, WeixinAdapter):
            adapter.disconnect()
            adapter.account_id = ""
            adapter.token = ""
            adapter.user_id = ""
            adapter._context_tokens = None  # type: ignore[attr-defined]
            adapter._processed_messages = None  # type: ignore[attr-defined]
            adapter._transport_state = {  # type: ignore[attr-defined]
                "mode": "polling",
                "status": "waiting_for_credentials",
                "connected": False,
                "last_error": "",
                "last_poll_outcome": "unbound",
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

        next_status = self._build_weixin_status_payload()
        return {
            "ok": True,
            "platform": "weixin",
            "account_id_configured": bool(account_id),
            "account_id_masked": _mask_secret(account_id) if account_id else "",
            "deleted_state_files": deleted,
            "configured": bool(next_status.get("configured")),
            "status": str(next_status.get("status") or ""),
        }

    def _project_weixin_login_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status") or "").strip()
        qrcode_url = str(payload.get("qrcode_img_content") or payload.get("qrcode_url") or "").strip()
        qrcode_value = str(payload.get("qrcode") or "").strip()
        expires_at_epoch = payload.get("expires_at_epoch")
        try:
            expires_in_seconds = max(0, int(float(expires_at_epoch) - time.time()))
        except (TypeError, ValueError):
            expires_in_seconds = 0
        return {
            "status": status or "not_started",
            "started_at": str(payload.get("started_at") or ""),
            "last_checked_at": str(payload.get("last_checked_at") or ""),
            "expires_in_seconds": expires_in_seconds,
            "qrcode_url": qrcode_url,
            "qrcode_available": bool(qrcode_value),
            "qr_svg_url": "/setup/weixin/qr.svg" if qrcode_value else "",
            "account_id_masked": _mask_secret(str(payload.get("account_id") or "")),
            "user_id_masked": _mask_secret(str(payload.get("user_id") or "")),
            "last_error": _redact_known_secrets(str(payload.get("last_error") or "")),
        }

    def build_weixin_login_qr_svg(self) -> str:
        login = self.store.load_weixin_login_state()
        qrcode_target = str(login.get("qrcode_img_content") or login.get("qrcode") or "").strip()
        if not qrcode_target:
            return _qr_to_svg("HarborGate Weixin login has not started")
        return _qr_to_svg(qrcode_target)

    def start_weixin_login(self, *, bot_type: str = "3") -> tuple[int, dict[str, Any]]:
        normalized_bot_type = str(bot_type or "3").strip() or "3"
        try:
            challenge = request_weixin_qr_challenge(bot_type=normalized_bot_type)
        except Exception as exc:  # noqa: BLE001 - setup API should return a usable blocker
            state = self.store.save_weixin_login_state(
                {
                    "status": "error",
                    "bot_type": normalized_bot_type,
                    "started_at": _now_utc(),
                    "last_checked_at": _now_utc(),
                    "last_error": str(exc),
                }
            )
            return 502, {
                "ok": False,
                "message": "failed to request Weixin login QR",
                "weixin_login": self._project_weixin_login_state(state),
            }

        state = self.store.save_weixin_login_state(
            {
                "status": "wait",
                "bot_type": normalized_bot_type,
                "qrcode": challenge.qrcode,
                "qrcode_img_content": challenge.qrcode_img_content,
                "current_base_url": ILINK_BASE_URL,
                "started_at": _now_utc(),
                "last_checked_at": "",
                "expires_at_epoch": time.time() + 480,
                "last_error": "",
            }
        )
        return 200, {
            "ok": True,
            "message": "Weixin login QR created",
            "weixin_login": self._project_weixin_login_state(state),
        }

    def poll_weixin_login(self) -> tuple[int, dict[str, Any]]:
        login = self.store.load_weixin_login_state()
        qrcode_value = str(login.get("qrcode") or "").strip()
        if not qrcode_value:
            return 404, {
                "ok": False,
                "message": "Weixin login has not started",
                "weixin_login": self._project_weixin_login_state(login),
            }

        expires_at_epoch = login.get("expires_at_epoch")
        try:
            expired = float(expires_at_epoch) <= time.time()
        except (TypeError, ValueError):
            expired = False
        if expired:
            login.update({"status": "expired", "last_checked_at": _now_utc()})
            saved = self.store.save_weixin_login_state(login)
            return 200, {
                "ok": False,
                "message": "Weixin login QR expired",
                "weixin_login": self._project_weixin_login_state(saved),
            }

        current_base_url = str(login.get("current_base_url") or ILINK_BASE_URL).strip() or ILINK_BASE_URL
        try:
            status = get_json(
                current_base_url,
                f"{EP_GET_QR_STATUS}?qrcode={parse.quote(qrcode_value)}",
                token=None,
                timeout_seconds=8,
            )
        except Exception as exc:  # noqa: BLE001 - expose redacted setup blocker
            error_text = str(exc)
            if "timed out" in error_text.lower() or "timeout" in error_text.lower():
                login.update(
                    {
                        "status": str(login.get("status") or "wait").strip() or "wait",
                        "last_checked_at": _now_utc(),
                        "last_error": "",
                    }
                )
                saved = self.store.save_weixin_login_state(login)
                return 200, {
                    "ok": False,
                    "message": "waiting for Weixin QR confirmation",
                    "weixin_login": self._project_weixin_login_state(saved),
                }
            login.update(
                {
                    "status": "error",
                    "last_checked_at": _now_utc(),
                    "last_error": error_text,
                }
            )
            saved = self.store.save_weixin_login_state(login)
            return 200, {
                "ok": False,
                "message": "failed to poll Weixin QR status",
                "weixin_login": self._project_weixin_login_state(saved),
            }

        qr_status = str(status.get("status") or "wait").strip() or "wait"
        login.update(
            {
                "status": qr_status,
                "last_checked_at": _now_utc(),
                "last_error": "",
            }
        )
        if qr_status == "scaned_but_redirect":
            redirect_host = str(status.get("redirect_host") or "").strip()
            if redirect_host:
                login["current_base_url"] = f"https://{redirect_host}"
        elif qr_status == "expired":
            login["expires_at_epoch"] = time.time()
        elif qr_status == "confirmed":
            account_id = str(status.get("ilink_bot_id") or "").strip()
            token = str(status.get("bot_token") or "").strip()
            base_url = str(status.get("baseurl") or ILINK_BASE_URL).strip() or ILINK_BASE_URL
            user_id = str(status.get("ilink_user_id") or "").strip()
            if not (account_id and token):
                login.update(
                    {
                        "status": "error",
                        "last_error": "Weixin confirmed login but the credential payload was incomplete",
                    }
                )
            else:
                save_weixin_account(
                    self.weixin_state_dir,
                    account_id=account_id,
                    token=token,
                    base_url=base_url,
                    user_id=user_id,
                )
                self.gateway.register_adapter(
                    WeixinAdapter(
                        state_dir=self.weixin_state_dir,
                        account_id=account_id,
                        token=token,
                        base_url=base_url,
                    )
                )
                login.update(
                    {
                        "account_id": account_id,
                        "user_id": user_id,
                        "current_base_url": base_url,
                    }
                )

        saved = self.store.save_weixin_login_state(login)
        return 200, {
            "ok": str(saved.get("status") or "") == "confirmed",
            "message": "Weixin login status updated",
            "weixin_login": self._project_weixin_login_state(saved),
        }

    def _discover_latest_json_report(self, report_path: Path) -> dict[str, Any]:
        if not report_path.exists():
            return {}
        if report_path.is_file():
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return {}
            if isinstance(payload, dict):
                payload = dict(payload)
                payload["_report_path"] = str(report_path)
                return payload
            return {}
        candidates = sorted(
            (path for path in report_path.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(payload, dict):
                payload = dict(payload)
                payload["_report_path"] = str(path)
                return payload
        return {}

    def _discover_latest_platform_live_gate_report(self) -> dict[str, Any]:
        for candidate in (
            self.runtime_root / "platform-live-gate",
            self.store.root / "platform-live-gate",
        ):
            payload = self._discover_latest_json_report(candidate)
            if payload:
                return payload
        return {}

    def _discover_latest_weixin_ingress_probe(self) -> dict[str, Any]:
        for candidate in (
            self.runtime_root / "weixin-ingress-probe",
            self.runtime_root / "weixin-ingress-probe-live.json",
            self.store.root / "weixin-ingress-probe",
            self.store.root / "weixin-ingress-probe-live.json",
        ):
            payload = self._discover_latest_json_report(candidate)
            if payload:
                return payload
        return {}

    def _effective_weixin_transport(
        self,
        *,
        adapter: WeixinAdapter,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        transport = adapter.transport_status() if hasattr(adapter, "transport_status") else {}
        transport = dict(transport) if isinstance(transport, dict) else {}
        account_id = str(record.get("account_id") or getattr(adapter, "account_id", "") or "").strip()
        if account_id:
            persisted = load_weixin_transport_state(self.weixin_state_dir, account_id)
            if isinstance(persisted, dict):
                transport.update(persisted)
                transport["mode"] = str(transport.get("mode") or "polling").strip() or "polling"

        token = str(record.get("token") or getattr(adapter, "token", "") or "").strip()
        context_tokens: list[str] = []
        if account_id:
            context_data = load_weixin_context_tokens(self.weixin_state_dir, account_id)
            context_tokens = [
                str(value).strip()
                for value in context_data.values()
                if str(value or "").strip()
            ]
        for key in ("last_error", "last_getupdates_error", "last_send_error"):
            transport[key] = _redact_known_secrets(
                str(transport.get(key) or ""),
                account_id,
                token,
                *context_tokens,
            )
        transport = _redact_status_payload(transport, account_id, token, *context_tokens)

        configured = bool(adapter.configured or record)
        if configured and not bool(transport.get("connected")):
            status = str(transport.get("status") or "").strip().lower()
            last_poll_outcome = str(transport.get("last_poll_outcome") or "").strip().lower()
            if last_poll_outcome in {"idle_timeout", "empty", "messages"}:
                transport["connected"] = True
                if not status or status == "polling":
                    transport["status"] = "polling_idle"
            elif status == "polling" and (
                str(transport.get("last_poll_at") or "").strip()
                or str(transport.get("last_getupdates_at") or "").strip()
            ):
                # Long-poll can legitimately stay "polling" while the transport is healthy.
                transport["connected"] = True
        return transport

    @staticmethod
    def _release_v2_delivery_policy() -> dict[str, str]:
        return {
            "interactive_reply": "source_bound",
            "proactive_delivery": "user-default-configured",
        }

    def _summarize_release_v2_status(
        self,
        *,
        feishu_ready: bool,
        weixin_ready: bool,
        parity_ready: bool,
        decision: str,
        decision_reason: str,
        weixin_blocker_category: str,
        delivery_health: dict[str, Any],
        latest_live_gate_report: dict[str, Any],
        ingress_proof: dict[str, Any],
    ) -> dict[str, Any]:
        source_bound_health = delivery_health.get("source_bound") if isinstance(delivery_health, dict) else {}
        source_bound_health = source_bound_health if isinstance(source_bound_health, dict) else {}
        proactive_health = delivery_health.get("proactive") if isinstance(delivery_health, dict) else {}
        proactive_health = proactive_health if isinstance(proactive_health, dict) else {}
        return {
            "delivery_policy": self._release_v2_delivery_policy(),
            "feishu_rehearsal_ready": bool(feishu_ready),
            "weixin_rehearsal_ready": bool(weixin_ready),
            "parity_ready": bool(parity_ready),
            "dual_surface_ready": bool(parity_ready),
            "decision": decision,
            "decision_reason": decision_reason,
            "weixin_blocker_category": weixin_blocker_category,
            "delivery_health": dict(delivery_health),
            "source_bound_delivery_health": dict(source_bound_health),
            "proactive_delivery_health": dict(proactive_health),
            "latest_platform_live_gate_report_path": str(latest_live_gate_report.get("_report_path") or ""),
            "weixin_ingress_proof": dict(ingress_proof),
            "release_v2_ready": bool(
                parity_ready
                and bool(source_bound_health.get("ready"))
                and bool(proactive_health.get("ready"))
            ),
        }

    def _build_weixin_status_payload(self, *, request_host: str = "") -> dict[str, Any]:
        record = self._discover_weixin_account_state()
        adapter = self.gateway.get_adapter("weixin")
        if not isinstance(adapter, WeixinAdapter):
            if record:
                account_id = str(record.get("account_id") or "").strip()
                token = str(record.get("token") or "").strip()
                adapter = WeixinAdapter(
                    state_dir=self.weixin_state_dir,
                    account_id=account_id,
                    token=token,
                    base_url=str(record.get("base_url") or "").strip() or None,
                )
            else:
                adapter = WeixinAdapter(state_dir=self.weixin_state_dir)

        context_token_count = 0
        context_tokens: list[str] = []
        if record:
            account_id = str(record.get("account_id") or "").strip()
            context_data = load_weixin_context_tokens(self.weixin_state_dir, account_id)
            context_tokens = [
                str(value).strip()
                for value in context_data.values()
                if str(value or "").strip()
            ]
            context_token_count = len(context_tokens)

        transport = self._effective_weixin_transport(adapter=adapter, record=record)
        configured = bool(adapter.configured or record)
        account_id = str(record.get("account_id") or getattr(adapter, "account_id", "") or "").strip()
        token = str(record.get("token") or getattr(adapter, "token", "") or "").strip()
        base_url = str(record.get("base_url") or getattr(adapter, "base_url", "") or "").strip()
        user_id = str(record.get("user_id") or getattr(adapter, "user_id", "") or "").strip()
        last_send_status = str(transport.get("last_send_status") or "").strip().lower()
        last_send_error = str(transport.get("last_send_error") or "").strip()
        ingress_proof = self._discover_latest_weixin_ingress_probe()
        if not ingress_proof:
            provider_private_text_count = int(transport.get("last_private_text_message_count") or 0)
            provider_private_text_seen = bool(
                provider_private_text_count > 0 or str(transport.get("last_inbound_message_id") or "").strip()
            )
            synthesized_blocked_reason = ""
            if configured and not provider_private_text_seen:
                synthesized_blocked_reason = "waiting_for_private_text"
            ingress_proof = {
                "provider_private_text_seen": provider_private_text_seen,
                "provider_private_text_count": provider_private_text_count,
                "blocked_reason": synthesized_blocked_reason,
                "transport": {
                    "status": str(transport.get("status") or "").strip(),
                    "connected": bool(transport.get("connected")),
                    "last_poll_outcome": str(transport.get("last_poll_outcome") or "").strip(),
                    "last_getupdates_at": str(transport.get("last_getupdates_at") or "").strip(),
                    "last_inbound_at": str(transport.get("last_inbound_at") or "").strip(),
                    "last_private_text_message_at": str(transport.get("last_private_text_message_at") or "").strip(),
                },
            }
        ingress_proof = _redact_status_payload(ingress_proof, account_id, token, *context_tokens)
        poll_status = str(transport.get("status") or "").strip().lower()
        poll_outcome = str(transport.get("last_poll_outcome") or "").strip().lower()
        poll_error = str(transport.get("last_getupdates_error") or transport.get("last_error") or "").strip()
        provider_private_text_seen = bool(ingress_proof.get("provider_private_text_seen"))
        provider_private_text_count = int(
            ingress_proof.get("provider_private_text_count") or transport.get("last_private_text_message_count") or 0
        )
        blocker_category = _classify_weixin_transport_blocker(
            configured=configured,
            connected=bool(transport.get("connected")),
            poll_status=poll_status,
            poll_outcome=poll_outcome,
            poll_error=poll_error,
            private_text_message_count=provider_private_text_count,
            private_text_confirmed=provider_private_text_seen,
            context_token_count=context_token_count,
            last_send_status=last_send_status,
            last_send_error=last_send_error,
        )
        ingress_blocker_category = _classify_weixin_ingress_blocker(
            configured=configured,
            connected=bool(transport.get("connected")),
            poll_status=poll_status,
            poll_error=poll_error,
            private_text_confirmed=provider_private_text_seen,
            context_token_count=context_token_count,
            last_send_status=last_send_status,
        )
        ingress_observability = {
            "poll_status": str(transport.get("status") or "").strip(),
            "last_poll_outcome": str(transport.get("last_poll_outcome") or "").strip(),
            "connected": bool(transport.get("connected")),
            "last_poll_at": str(transport.get("last_poll_at") or "").strip(),
            "last_getupdates_at": str(transport.get("last_getupdates_at") or "").strip(),
            "last_getupdates_buf": (
                "[REDACTED]" if str(transport.get("last_getupdates_buf") or "").strip() else ""
            ),
            "last_getupdates_count": int(transport.get("last_getupdates_count") or 0),
            "last_private_text_message_count": int(transport.get("last_private_text_message_count") or 0),
            "last_private_text_message_at": str(transport.get("last_private_text_message_at") or "").strip(),
            "last_getupdates_message_ids": list(transport.get("last_getupdates_message_ids") or []),
            "last_getupdates_private_message_ids": list(transport.get("last_getupdates_private_message_ids") or []),
            "last_getupdates_error": str(transport.get("last_getupdates_error") or "").strip(),
            "last_inbound_at": str(transport.get("last_inbound_at") or "").strip(),
            "last_inbound_message_id": str(transport.get("last_inbound_message_id") or "").strip(),
            "last_inbound_chat_id": str(transport.get("last_inbound_chat_id") or "").strip(),
            "provider_private_text_seen": bool(ingress_proof.get("provider_private_text_seen")),
            "provider_private_text_count": int(ingress_proof.get("provider_private_text_count") or 0),
            "blocked_reason": str(ingress_proof.get("blocked_reason") or "").strip(),
            "report_path": str(ingress_proof.get("_report_path") or "").strip(),
        }
        outbound_observability = {
            "last_send_at": str(transport.get("last_send_at") or "").strip(),
            "last_send_status": last_send_status,
            "last_send_chunk_count": int(transport.get("last_send_chunk_count") or 0),
            "last_send_retryable": bool(transport.get("last_send_retryable")),
            "last_send_provider_message_id": str(transport.get("last_send_provider_message_id") or "").strip(),
            "last_send_context_token_used": bool(transport.get("last_send_context_token_used")),
            "last_send_error": str(transport.get("last_send_error") or "").strip(),
        }

        return {
            "configured": configured,
            "account_id": account_id,
            "account_id_masked": _mask_secret(account_id) if account_id else "",
            "base_url": base_url,
            "user_id": user_id,
            "user_id_masked": _mask_secret(user_id) if user_id else "",
            "status": str(transport.get("status") or ("waiting_for_credentials" if not configured else "polling_idle")).strip(),
            "connected": bool(transport.get("connected")),
            "blocker_category": blocker_category,
            "ingress_blocker_category": ingress_blocker_category,
            "poll": {
                "status": str(transport.get("status") or "").strip(),
                "outcome": str(transport.get("last_poll_outcome") or "").strip(),
                "last_poll_at": str(transport.get("last_poll_at") or "").strip(),
                "last_getupdates_at": str(transport.get("last_getupdates_at") or "").strip(),
                "last_getupdates_buf": (
                    "[REDACTED]" if str(transport.get("last_getupdates_buf") or "").strip() else ""
                ),
                "last_getupdates_count": int(transport.get("last_getupdates_count") or 0),
                "last_private_text_message_count": int(transport.get("last_private_text_message_count") or 0),
                "last_private_text_message_at": str(transport.get("last_private_text_message_at") or "").strip(),
                "error": poll_error,
            },
            "context_token_count": context_token_count,
            "last_context_token_at": str(transport.get("last_context_token_at") or "").strip(),
            "last_send_at": str(transport.get("last_send_at") or "").strip(),
            "last_send_chunk_count": int(transport.get("last_send_chunk_count") or 0),
            "last_private_text_message_at": str(transport.get("last_private_text_message_at") or "").strip(),
            "last_inbound_at": str(transport.get("last_inbound_at") or "").strip(),
            "last_inbound_message_id": str(transport.get("last_inbound_message_id") or "").strip(),
            "last_inbound_chat_id": str(transport.get("last_inbound_chat_id") or "").strip(),
            "last_error": str(transport.get("last_error") or "").strip(),
            "ingress_proof": ingress_proof,
            "ingress_observability": ingress_observability,
            "delivery_observability": outbound_observability,
            "transport_mode": "polling",
        }

    def ensure_feishu_adapter(self) -> FeishuAdapter:
        adapter = self.gateway.get_adapter("feishu")
        if isinstance(adapter, FeishuAdapter):
            return adapter
        adapter = FeishuAdapter()
        self.gateway.register_adapter(adapter)
        return adapter

    @staticmethod
    def _redacted_feishu_transport(adapter: FeishuAdapter) -> dict[str, Any]:
        transport = adapter.transport_status()
        transport = dict(transport) if isinstance(transport, dict) else {}
        return _redact_status_payload(
            transport,
            adapter.settings.app_id,
            adapter.settings.app_secret,
            adapter.settings.verification_token,
            adapter.settings.encrypt_key,
        )

    @staticmethod
    def _redacted_readiness(
        *,
        configured: bool,
        connected: bool,
        status: str,
        blocked_reason: str = "",
    ) -> dict[str, Any]:
        normalized_status = str(status or "").strip()
        normalized_blocked_reason = str(blocked_reason or "").strip()
        if configured and connected and not normalized_blocked_reason:
            state = "ready"
            reason = ""
        elif not configured:
            state = "not_configured"
            reason = normalized_blocked_reason or "credentials_missing"
        elif normalized_blocked_reason:
            state = "blocked"
            reason = normalized_blocked_reason
        elif normalized_status.lower() in {"error", "timeout", "send_failed"}:
            state = "blocked"
            reason = normalized_status
        else:
            state = "degraded"
            reason = normalized_status or "not_connected"
        return {
            "ready": state == "ready",
            "state": state,
            "reason": reason,
            "configured": bool(configured),
            "connected": bool(connected),
            "status": normalized_status,
        }

    @staticmethod
    def _weixin_qr_status(weixin: dict[str, Any]) -> str:
        blocker = str(
            weixin.get("ingress_blocker_category")
            or weixin.get("blocker_category")
            or ""
        ).strip()
        if not bool(weixin.get("configured")) or blocker == "account_restore":
            return "login_required"
        if blocker in {"qr_recovery", "weixin_provider_auth_failed"}:
            return "recovery_required"
        return "configured"

    def _platform_setup_urls(self, *, origin: str, session_code: str) -> dict[str, dict[str, str]]:
        feishu_setup_url = self.build_feishu_setup_url(origin=origin, session_code=session_code)
        return {
            "feishu": {
                "manage_url": f"{origin}/admin/im/feishu",
                "setup_url": feishu_setup_url,
                "static_setup_url": f"{origin}/setup/feishu",
                "qr_page_url": f"{origin}/setup/feishu/qr",
                "qr_svg_url": f"{origin}/setup/feishu/qr.svg",
            },
            "weixin": {
                "manage_url": f"{origin}/admin/im/weixin",
                "setup_url": f"{origin}/setup/weixin",
                "static_setup_url": f"{origin}/setup/weixin",
                "qr_page_url": "",
                "qr_svg_url": "",
            },
        }

    def build_status_payload(self, *, request_host: str = "") -> dict[str, Any]:
        state = self.store.load_state()
        adapter = self.ensure_feishu_adapter()
        feishu_state = state.get("feishu") or {}
        if not isinstance(feishu_state, dict):
            feishu_state = {}
        self._bootstrap_weixin_adapter()
        weixin = self._build_weixin_status_payload(request_host=request_host)
        gateway_status = self.build_gateway_status_payload(request_host=request_host)
        delivery_health = self.gateway.store.summarize_delivery_health()
        live_gate_report = self._discover_latest_platform_live_gate_report()
        release_v2 = self._summarize_release_v2_status(
            feishu_ready=bool(
                bool(feishu_state.get("app_id"))
                and bool(feishu_state.get("app_secret") or adapter.settings.app_secret)
                and bool(adapter.transport_status().get("connected"))
            ),
            weixin_ready=bool(weixin.get("configured")) and not bool(weixin.get("blocker_category")),
            parity_ready=bool(gateway_status.get("release_v2", {}).get("parity_ready")),
            decision=str(live_gate_report.get("decision") or gateway_status.get("release_v2", {}).get("decision") or "").strip(),
            decision_reason=str(live_gate_report.get("decision_reason") or gateway_status.get("release_v2", {}).get("decision_reason") or "").strip(),
            weixin_blocker_category=str(
                weixin.get("ingress_blocker_category")
                or weixin.get("blocker_category")
                or weixin.get("blocked_reason")
                or ""
            ).strip(),
            delivery_health=delivery_health,
            latest_live_gate_report=live_gate_report,
            ingress_proof=weixin.get("ingress_proof") if isinstance(weixin.get("ingress_proof"), dict) else {},
        )

        origin = self.resolve_public_origin(request_host=request_host)
        session_code = str(state.get("session_code") or "")
        platform_urls = self._platform_setup_urls(origin=origin, session_code=session_code)
        feishu_urls = platform_urls["feishu"]
        weixin_urls = platform_urls["weixin"]
        setup_url = self.build_setup_url(origin=origin, session_code=session_code)
        static_setup_url = f"{origin}/setup"
        manage_url = f"{origin}/admin/im"
        qr_page_url = f"{origin}/setup/qr"
        qr_svg_url = f"{origin}/setup/qr.svg"
        webhook_url = ""
        if adapter.settings.connection_mode == "webhook":
            webhook_url = parse.urljoin(f"{origin}/", adapter.webhook_path.lstrip("/"))
        transport = self._redacted_feishu_transport(adapter)

        current_app_id = str(feishu_state.get("app_id") or adapter.settings.app_id or "").strip()
        current_app_name = str(feishu_state.get("app_name") or adapter.settings.bot_name or "").strip()
        configured = bool(current_app_id and (feishu_state.get("app_secret") or adapter.settings.app_secret))
        connected = bool(transport.get("connected"))
        transport_status = str(transport.get("status") or ("waiting_for_credentials" if not configured else "ready")).strip()
        feishu_readiness = self._redacted_readiness(
            configured=configured,
            connected=connected,
            status=transport_status,
            blocked_reason="" if configured else "credentials_missing",
        )
        weixin_readiness = self._redacted_readiness(
            configured=bool(weixin.get("configured")),
            connected=bool(weixin.get("connected")),
            status=str(weixin.get("status") or ""),
            blocked_reason=str(
                weixin.get("ingress_blocker_category")
                or weixin.get("blocker_category")
                or ""
            ),
        )
        weixin_login = self._project_weixin_login_state(self.store.load_weixin_login_state())
        return {
            "session_code": session_code,
            "setup_url": setup_url,
            "static_setup_url": static_setup_url,
            "manage_url": manage_url,
            "qr_page_url": qr_page_url,
            "qr_svg_url": qr_svg_url,
            "qr_page_path": "/setup/qr",
            "qr_svg_path": "/setup/qr.svg",
            "webhook_url": webhook_url,
            "public_origin": origin,
            "mobile_reachable": self._is_mobile_reachable(origin),
            "feishu": {
                "configured": configured,
                "connected": connected,
                "app_id_masked": _mask_secret(current_app_id) if current_app_id else "",
                "app_name": current_app_name,
                "tenant_key": str(feishu_state.get("tenant_key") or "").strip(),
                "bot_open_id": str(feishu_state.get("bot_open_id") or adapter.settings.bot_open_id or "").strip(),
                "bot_user_id": str(feishu_state.get("bot_user_id") or adapter.settings.bot_user_id or "").strip(),
                "status": transport_status,
                "credential_status": str(feishu_state.get("status") or ("validated" if configured else "not_configured")).strip(),
                "transport_status": transport_status,
                "last_validated_at": str(feishu_state.get("last_validated_at") or "").strip(),
                "last_connected_at": str(transport.get("last_connected_at") or "").strip(),
                "last_event_at": str(transport.get("last_event_at") or "").strip(),
                "last_error": str(transport.get("last_error") or "").strip(),
                "connection_mode": adapter.settings.connection_mode,
                "enable_live_send": adapter.settings.enable_live_send,
                "thread_alive": bool(transport.get("thread_alive")),
                "verification_token_configured": bool(
                    str(feishu_state.get("verification_token") or adapter.settings.verification_token or "").strip()
                ),
                "api_key_configured": configured,
                **feishu_urls,
                "readiness": feishu_readiness,
            },
            "weixin": {
                **weixin,
                "qr_status": self._weixin_qr_status(weixin),
                "login": weixin_login,
                "manage_status": "available",
                **weixin_urls,
                "readiness": weixin_readiness,
            },
            "connectors": {
                "feishu": dict(feishu_urls),
                "weixin": dict(weixin_urls),
            },
            "gateway_status": gateway_status,
            "channels": gateway_status.get("channels", []),
            "delivery_policy": self._release_v2_delivery_policy(),
            "delivery_health": delivery_health,
            "release_v2": release_v2,
        }

    def build_gateway_status_payload(self, *, request_host: str = "") -> dict[str, Any]:
        origin = self.resolve_public_origin(request_host=request_host)
        state = self.store.load_state()
        session_code = str(state.get("session_code") or "")
        platform_urls = self._platform_setup_urls(origin=origin, session_code=session_code)
        feishu_state = state.get("feishu") or {}
        if not isinstance(feishu_state, dict):
            feishu_state = {}
        feishu_adapter = self.ensure_feishu_adapter()
        self._bootstrap_weixin_adapter()
        weixin = self._build_weixin_status_payload(request_host=request_host)
        channels: list[dict[str, Any]] = []
        weixin_record = self._discover_weixin_account_state()
        for adapter_name, adapter in self.gateway._adapters.items():
            if isinstance(adapter, WeixinAdapter):
                transport = self._effective_weixin_transport(adapter=adapter, record=weixin_record)
            elif isinstance(adapter, FeishuAdapter):
                transport = self._redacted_feishu_transport(adapter)
            else:
                transport = adapter.transport_status() if hasattr(adapter, "transport_status") else {}
                transport = transport if isinstance(transport, dict) else {}
                transport = _redact_status_payload(transport)
            profile = adapter.get_profile() if hasattr(adapter, "get_profile") else {}
            profile = profile if isinstance(profile, dict) else {}
            adapter_settings = getattr(adapter, "settings", None)
            transport_mode = str(
                transport.get("mode")
                or profile.get("transport_mode")
                or getattr(adapter_settings, "connection_mode", "")
                or ""
            ).strip()
            channel = {
                "platform": str(getattr(adapter, "name", adapter_name) or adapter_name).strip(),
                "enabled": True,
                "connected": bool(transport.get("connected")),
                "display_name": "",
                "surface_family": str(profile.get("surface_family") or "").strip(),
                "placeholder": bool(profile.get("placeholder")),
                "capabilities": {
                    "reply": bool(profile.get("supports_replies")),
                    "update": bool(profile.get("supports_updates")),
                    "attachments": bool(profile.get("supports_attachments")),
                },
            }
            configured_attr = getattr(adapter, "configured", True)
            channel["enabled"] = bool(configured_attr) or bool(
                profile.get("placeholder")
            )
            channel["transport"] = {
                "mode": transport_mode,
                "status": str(transport.get("status") or "").strip(),
                "connected": bool(transport.get("connected")),
                "thread_alive": bool(transport.get("thread_alive")),
                "last_connected_at": str(transport.get("last_connected_at") or "").strip(),
                "last_event_at": str(transport.get("last_event_at") or "").strip(),
                "last_error": str(transport.get("last_error") or "").strip(),
            }
            if isinstance(adapter, FeishuAdapter):
                channel["display_name"] = str(adapter.settings.bot_name or "Feishu").strip()
                channel["capabilities"] = {
                    "reply": bool(adapter.configured and adapter.settings.enable_live_send),
                    "update": False,
                    "attachments": False,
                }
                channel.update(platform_urls["feishu"])
            elif adapter_name == "webhook":
                channel["display_name"] = "Webhook"
            else:
                channel["display_name"] = (
                    str(profile.get("display_name") or "")
                    or str(getattr(adapter, "name", adapter_name) or adapter_name).strip().title()
                )
                platform_key = str(channel.get("platform") or adapter_name).strip().lower()
                if platform_key in platform_urls:
                    channel.update(platform_urls[platform_key])
            channels.append(channel)

        delivery_observability = self.gateway.store.summarize_delivery_records()
        delivery_health = self.gateway.store.summarize_delivery_health()
        live_gate_report = self._discover_latest_platform_live_gate_report()
        current_app_id = str(feishu_state.get("app_id") or feishu_adapter.settings.app_id or "").strip()
        current_app_secret_present = bool(feishu_state.get("app_secret") or feishu_adapter.settings.app_secret)
        feishu_configured = bool(current_app_id and current_app_secret_present)
        feishu_transport = self._redacted_feishu_transport(feishu_adapter)
        feishu_connected = bool(feishu_transport.get("connected"))
        feishu_status = str(
            feishu_transport.get("status")
            or ("waiting_for_credentials" if not feishu_configured else "ready")
        ).strip()
        feishu_readiness = self._redacted_readiness(
            configured=feishu_configured,
            connected=feishu_connected,
            status=feishu_status,
            blocked_reason="" if feishu_configured else "credentials_missing",
        )
        weixin_readiness = self._redacted_readiness(
            configured=bool(weixin.get("configured")),
            connected=bool(weixin.get("connected")),
            status=str(weixin.get("status") or ""),
            blocked_reason=str(
                weixin.get("ingress_blocker_category")
                or weixin.get("blocker_category")
                or ""
            ),
        )
        release_v2 = self._summarize_release_v2_status(
            feishu_ready=any(
                bool(channel.get("connected")) and str(channel.get("platform") or "").lower() == "feishu"
                for channel in channels
            ),
            weixin_ready=any(
                bool(channel.get("connected")) and str(channel.get("platform") or "").lower() == "weixin"
                for channel in channels
            ),
            parity_ready=bool(live_gate_report.get("parity_ready")),
            decision=str(live_gate_report.get("decision") or "").strip(),
            decision_reason=str(live_gate_report.get("decision_reason") or "").strip(),
            weixin_blocker_category=str(live_gate_report.get("weixin_blocker_category") or "").strip(),
            delivery_health=delivery_health,
            latest_live_gate_report=live_gate_report,
            ingress_proof=weixin.get("ingress_proof") if isinstance(weixin.get("ingress_proof"), dict) else {},
        )

        return {
            "ok": True,
            "gateway_version": __version__,
            "gateway_base_url": origin,
            "manage_url": f"{origin}/admin/im",
            "setup_url": self.build_setup_url(
                origin=origin,
                session_code=session_code,
            ),
            "static_setup_url": f"{origin}/setup",
            "qr_page_url": f"{origin}/setup/qr",
            "qr_svg_url": f"{origin}/setup/qr.svg",
            "channels": channels,
            "connectors": {
                "feishu": dict(platform_urls["feishu"]),
                "weixin": dict(platform_urls["weixin"]),
            },
            "feishu": {
                "configured": feishu_configured,
                "api_key_configured": feishu_configured,
                "connected": feishu_connected,
                "status": feishu_status,
                "credential_status": str(
                    feishu_state.get("status")
                    or ("validated" if feishu_configured else "not_configured")
                ).strip(),
                "connection_mode": feishu_adapter.settings.connection_mode,
                "enable_live_send": bool(feishu_adapter.settings.enable_live_send),
                "display_name": str(feishu_adapter.settings.bot_name or "Feishu").strip(),
                "app_id_masked": _mask_secret(current_app_id) if current_app_id else "",
                **platform_urls["feishu"],
                "readiness": feishu_readiness,
            },
            "weixin": {
                "configured": bool(weixin.get("configured")),
                "connected": bool(weixin.get("connected")),
                "status": str(weixin.get("status") or "").strip(),
                "blocker_category": str(weixin.get("blocker_category") or "").strip(),
                "ingress_blocker_category": str(weixin.get("ingress_blocker_category") or "").strip(),
                "qr_status": self._weixin_qr_status(weixin),
                "login": self._project_weixin_login_state(self.store.load_weixin_login_state()),
                "manage_status": "available",
                **platform_urls["weixin"],
                "readiness": weixin_readiness,
                "poll": dict(weixin.get("poll") or {}) if isinstance(weixin.get("poll"), dict) else {},
                "delivery_observability": (
                    dict(weixin.get("delivery_observability") or {})
                    if isinstance(weixin.get("delivery_observability"), dict)
                    else {}
                ),
            },
            "delivery_observability": delivery_observability,
            "delivery_health": delivery_health,
            "delivery_policy": self._release_v2_delivery_policy(),
            "release_v2": release_v2,
        }

    def build_setup_page(self, *, request_host: str = "") -> str:
        status = self.build_status_payload(request_host=request_host)
        feishu = status["feishu"]
        weixin = status["weixin"]
        gateway_status = status["gateway_status"] if isinstance(status.get("gateway_status"), dict) else {}
        gateway_channels = gateway_status.get("channels") if isinstance(gateway_status, dict) else []
        setup_url = _html_escape(str(feishu.get("setup_url") or status["setup_url"]))
        qr_path = _html_escape("/setup/feishu/qr.svg")
        session_code = _html_escape(str(status["session_code"]))
        app_name = _html_escape(str(feishu["app_name"]))
        app_id_masked = _html_escape(str(feishu["app_id_masked"]))
        state_text = _html_escape(str(feishu["status"]))
        connection_mode = _html_escape(str(feishu["connection_mode"]))
        credential_status = _html_escape(str(feishu["credential_status"]))
        last_error = _html_escape(str(feishu["last_error"]))
        mode_hint = (
            '<p class="hint ok">当前默认使用飞书长连接模式，不需要公网回调地址。</p>'
            if feishu["connection_mode"] == "websocket"
            else '<p class="hint">当前处于 webhook 模式，需要公网可达的回调地址。</p>'
        )
        transport_meta = ""
        if last_error:
            transport_meta += f'<div><strong>最近错误：</strong><code>{last_error}</code></div>'
        warning = ""
        if not status["mobile_reachable"]:
            warning = (
                '<p class="hint err">当前二维码链接看起来仍然是本机回环地址。'
                '如果手机扫不开，请用 IM_AGENT_HOST=0.0.0.0 启动，或设置 IM_AGENT_PUBLIC_ORIGIN。</p>'
            )
        gateway_rows = ""
        if isinstance(gateway_channels, list):
            for channel in gateway_channels:
                if not isinstance(channel, dict):
                    continue
                platform = _html_escape(str(channel.get("platform") or ""))
                display_name = _html_escape(str(channel.get("display_name") or ""))
                connected = "yes" if bool(channel.get("connected")) else "no"
                transport = channel.get("transport") if isinstance(channel.get("transport"), dict) else {}
                transport_status = _html_escape(str(transport.get("status") or ""))
                surface_family = _html_escape(str(channel.get("surface_family") or ""))
                gateway_rows += (
                    f'<div><strong>{platform or "unknown"}</strong>'
                    f' <span class="hint">({display_name or "未命名"} / {surface_family or "unknown"})</span>'
                    f'<br /><span class="hint">connected={connected}, transport={transport_status or "unknown"}</span></div>'
                )
        gateway_version = _html_escape(str(gateway_status.get("gateway_version") or __version__))
        release_v2 = status.get("release_v2") if isinstance(status.get("release_v2"), dict) else {}
        release_decision = _html_escape(str(release_v2.get("decision") or ""))
        release_reason = _html_escape(str(release_v2.get("decision_reason") or ""))
        source_bound_health = release_v2.get("source_bound_delivery_health") if isinstance(release_v2.get("source_bound_delivery_health"), dict) else {}
        proactive_health = release_v2.get("proactive_delivery_health") if isinstance(release_v2.get("proactive_delivery_health"), dict) else {}
        source_bound_health_state = _html_escape(str(source_bound_health.get("health_state") or "unknown"))
        proactive_health_state = _html_escape(str(proactive_health.get("health_state") or "unknown"))
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HarborGate Feishu 配置</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f4efe7; color: #1e1b18; margin: 0; }}
    .wrap {{ max-width: 620px; margin: 0 auto; padding: 24px 18px 48px; }}
    .card {{ background: rgba(255,255,255,0.92); border-radius: 20px; padding: 20px; box-shadow: 0 18px 48px rgba(51,36,18,0.12); }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    p {{ line-height: 1.55; }}
    label {{ display: block; margin: 14px 0 8px; font-weight: 600; }}
    input {{ width: 100%; box-sizing: border-box; padding: 14px 12px; border-radius: 12px; border: 1px solid #d9c6ae; font-size: 16px; }}
    input[readonly] {{ background: #f6f2ec; color: #6a5d50; }}
    button {{ width: 100%; margin-top: 18px; padding: 14px 16px; border: 0; border-radius: 999px; background: #1f7a6f; color: white; font-size: 16px; font-weight: 700; }}
    .meta {{ color: #6b5a49; font-size: 14px; margin-bottom: 18px; }}
    .status {{ margin: 16px 0; padding: 12px 14px; border-radius: 14px; background: #f6f2ec; }}
    .hint {{ font-size: 13px; color: #766757; }}
    .ok {{ color: #1f7a6f; }}
    .err {{ color: #b94739; }}
    .qr {{ margin: 18px auto 6px; width: 220px; height: 220px; display: block; border-radius: 16px; background: #f8f3ec; }}
    code {{ word-break: break-all; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="meta">HarborGate · Feishu 手机配置页</div>
      <h1>扫码后直接填飞书凭证</h1>
      <p>这个页面会把 <code>app_id</code> 和 <code>app_secret</code> 保存在当前 HarborGate 本机，并立即更新正在运行的 Feishu adapter，不需要用户手动登录到服务器。</p>
      <img class="qr" src="{qr_path}" alt="setup qr" />
      <p class="hint">当前配置链接：<code>{setup_url}</code></p>
      {warning}
      {mode_hint}
      <div class="status">
        <div><strong>连接状态：</strong>{state_text}</div>
        <div><strong>凭证状态：</strong>{credential_status}</div>
        <div><strong>接收模式：</strong>{connection_mode}</div>
        <div><strong>当前会话：</strong>{session_code}</div>
        <div><strong>Bot 显示名：</strong>{app_name or "未配置"}</div>
        <div><strong>App ID：</strong>{app_id_masked or "未配置"}</div>
        {transport_meta}
      </div>
      <label for="app-id">App ID</label>
      <input id="app-id" type="text" placeholder="cli_xxx" />
      <label for="app-secret">App Secret</label>
      <input id="app-secret" type="password" placeholder="输入飞书应用密钥" />
      <label for="verification-token">Verification Token（可选，仅 webhook 模式才需要）</label>
      <input id="verification-token" type="text" placeholder="长连接模式通常不需要填写" />
      <button id="submit-btn">验证并应用 Feishu 配置</button>
      <p class="hint">当前 starter 会先验证凭证，再把配置写入本地状态文件，并默认启用 live send + 飞书长连接收消息。</p>
      <p id="result" class="hint"></p>
    </div>
    <div class="card" style="margin-top: 18px;">
      <div class="meta">HarborGate · Weixin 状态</div>
      <h2 style="margin: 0 0 8px; font-size: 20px;">Weixin parity / getupdates</h2>
      <p>Weixin 仍然通过 QR 登录和 getupdates 长轮询运行。这个卡片只展示已脱敏状态，方便 HarborDesk 在同一页里看 setup、gateway 和 ingress 健康度。</p>
      <div class="status">
        <div><strong>连接状态：</strong>{_html_escape(str(weixin["status"]))}</div>
        <div><strong>账号状态：</strong>{_html_escape(str(weixin["account_id_masked"] or "未配置"))}</div>
        <div><strong>Transport blocker：</strong>{_html_escape(str(weixin["blocker_category"] or "ready"))}</div>
        <div><strong>Parity bucket：</strong>{_html_escape(str(weixin["ingress_blocker_category"] or "ready"))}</div>
        <div><strong>最近 getupdates：</strong>{_html_escape(str(weixin["poll"]["last_getupdates_at"] or "暂无"))}</div>
        <div><strong>私聊消息：</strong>{_html_escape(str(weixin["poll"]["last_private_text_message_count"]))}</div>
        <div><strong>context_token：</strong>{_html_escape(str(weixin["context_token_count"]))}</div>
        <div><strong>ingress_observability：</strong>{_html_escape(str(weixin["ingress_observability"]["last_getupdates_count"]))} / {_html_escape(str(weixin["ingress_observability"]["last_inbound_message_id"] or "暂无"))}</div>
        <div><strong>outbound_observability：</strong>{_html_escape(str(weixin["delivery_observability"]["last_send_status"] or "idle"))} / {_html_escape(str(weixin["delivery_observability"]["last_send_error"] or "无错误"))}</div>
      </div>
      <p class="hint">登录入口：<code>harborgate-weixin-login</code>。收消息 runtime 由 <code>harborgate.service</code> 进程内 supervisor 启动；排查 ingress 时用 <code>harborgate-weixin-ingress-probe</code>。长轮询空闲会表现为 <code>idle_timeout</code>，这不再视为故障。</p>
    </div>
    <div class="card" style="margin-top: 18px;">
      <div class="meta">HarborGate · Gateway 状态</div>
      <h2 style="margin: 0 0 8px; font-size: 20px;">Redacted channel snapshot</h2>
      <div class="status">
        <div><strong>gateway_version：</strong>{gateway_version}</div>
        <div><strong>channels：</strong>{len(gateway_channels) if isinstance(gateway_channels, list) else 0}</div>
        <div><strong>release_v2：</strong>{release_decision or "unknown"}<span class="hint"> / {release_reason or "n/a"}</span></div>
        <div><strong>source_bound_health：</strong>{source_bound_health_state}</div>
        <div><strong>proactive_health：</strong>{proactive_health_state}</div>
        {gateway_rows or '<div class="hint">当前还没有注册到网关的通道。</div>'}
      </div>
    </div>
  </div>
  <script>
    document.getElementById('submit-btn').addEventListener('click', async () => {{
      const result = document.getElementById('result');
      result.className = 'hint';
      result.textContent = '正在验证 Feishu 凭证...';
      const payload = {{
        session_code: {json.dumps(status["session_code"], ensure_ascii=False)},
        app_id: document.getElementById('app-id').value.trim(),
        app_secret: document.getElementById('app-secret').value.trim(),
        verification_token: document.getElementById('verification-token').value.trim(),
      }};
      try {{
        const response = await fetch('/api/setup/feishu/configure', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const data = await response.json();
        if (!response.ok || !data.success) {{
          throw new Error(data.message || data.error || '配置失败');
        }}
        result.className = 'hint ok';
        const appName = data.bot_info?.app_name || 'Feishu Bot';
        result.textContent = `已应用配置：${{appName}}。下一步去飞书后台把事件订阅方式切到“使用长连接接收事件”，并订阅 im.message.receive_v1。`;
      }} catch (error) {{
        result.className = 'hint err';
        result.textContent = error.message;
      }}
    }});
  </script>
</body>
</html>"""

    def build_weixin_setup_page(self, *, request_host: str = "", unbound: bool = False) -> str:
        status = self.build_status_payload(request_host=request_host)
        weixin = status["weixin"]
        qr_status = _html_escape(str(weixin.get("qr_status") or "unknown"))
        state_text = _html_escape(str(weixin.get("status") or "unknown"))
        account_id = _html_escape(str(weixin.get("account_id_masked") or "未配置"))
        user_id = _html_escape(str(weixin.get("user_id_masked") or "未配置"))
        blocker = _html_escape(str(weixin.get("ingress_blocker_category") or weixin.get("blocker_category") or "ready"))
        last_poll = _html_escape(str((weixin.get("poll") or {}).get("last_getupdates_at") or "暂无"))
        private_count = _html_escape(str((weixin.get("poll") or {}).get("last_private_text_message_count") or 0))
        context_count = _html_escape(str(weixin.get("context_token_count") or 0))
        last_error = _html_escape(str(weixin.get("last_error") or (weixin.get("poll") or {}).get("error") or ""))
        error_row = f"<div><strong>最近错误：</strong><code>{last_error}</code></div>" if last_error else ""
        login_state_json = json.dumps(
            self._project_weixin_login_state(self.store.load_weixin_login_state()),
            ensure_ascii=False,
        )
        unbind_disabled = "" if bool(weixin.get("configured")) else " disabled"
        unbound_notice = (
            '<div class="notice ok"><strong>已解绑。</strong>本机保存的 Weixin 账号和轮询状态已清除，'
            "请在本页重新生成二维码并完成扫码登录。</div>"
            if unbound
            else ""
        )
        weixin_configured = bool(weixin.get("configured"))
        login_hint = (
            "当前已绑定 Weixin 账号，收消息由 harborgate.service 内部 runtime 轮询。"
            "如需换绑，再生成新的微信扫码登录二维码。"
            if weixin_configured
            else "如果状态是 login_required，请点击按钮生成二维码。"
        )
        login_button_text = "重新生成微信扫码登录二维码" if weixin_configured else "生成微信扫码登录二维码"
        configured_notice = (
            '<div class="notice ok"><strong>已绑定。</strong>HarborGate 已保存本机 Weixin 账号状态；'
            "返回 HarborDesk 后应显示 polling/connected。需要更换账号时再使用下面的重新扫码入口。</div>"
            if weixin_configured
            else ""
        )
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HarborGate Weixin 配置</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f4efe7; color: #1e1b18; margin: 0; }}
    .wrap {{ max-width: 620px; margin: 0 auto; padding: 24px 18px 48px; }}
    .card {{ background: rgba(255,255,255,0.92); border-radius: 20px; padding: 20px; box-shadow: 0 18px 48px rgba(51,36,18,0.12); }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    p {{ line-height: 1.55; }}
    .meta {{ color: #6b5a49; font-size: 14px; margin-bottom: 18px; }}
    .status {{ margin: 16px 0; padding: 12px 14px; border-radius: 14px; background: #f6f2ec; }}
    .hint {{ font-size: 13px; color: #766757; }}
    .ok {{ color: #1f7a6f; }}
    .err {{ color: #b94739; }}
    .notice {{ margin: 14px 0; padding: 12px 14px; border-radius: 14px; background: #edf7f3; }}
    .login-panel {{ margin-top: 18px; padding: 14px; border-radius: 16px; background: #fbf8f2; border: 1px solid #e8ded1; }}
    .login-qr {{ width: 260px; height: 260px; display: none; margin: 14px 0 6px; background: #f8f3ec; border-radius: 14px; }}
    code {{ word-break: break-all; }}
    form {{ margin: 18px 0 0; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    button {{ border: 0; border-radius: 999px; padding: 10px 16px; font-weight: 700; cursor: pointer; }}
    button.primary {{ background: #1f7a6f; color: #fff; }}
    button.danger {{ background: #b3261e; color: #fff; }}
    button:disabled {{ background: #d7d2ca; color: #817a72; cursor: not-allowed; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="meta">HarborGate · Weixin 管理页</div>
      <h1>微信配置与登录状态</h1>
      <p>微信凭据和扫码登录由 HarborGate 的 Weixin adapter 管理。这个页面只展示脱敏状态，不接收或保存微信 token。</p>
      {unbound_notice}
      {configured_notice}
      <div class="status">
        <div><strong>连接状态：</strong>{state_text}</div>
        <div><strong>扫码状态：</strong>{qr_status}</div>
        <div><strong>账号：</strong>{account_id}</div>
        <div><strong>用户：</strong>{user_id}</div>
        <div><strong>阻塞项：</strong>{blocker}</div>
        <div><strong>最近 getupdates：</strong>{last_poll}</div>
        <div><strong>私聊消息：</strong>{private_count}</div>
        <div><strong>context_token：</strong>{context_count}</div>
        {error_row}
      </div>
      <div class="login-panel">
        <button id="weixin-login-start" class="primary" type="button">{login_button_text}</button>
        <div id="weixin-login-status" class="hint" style="margin-top: 10px;">{login_hint}</div>
        <img id="weixin-login-qr" class="login-qr" src="" alt="Weixin login QR" />
        <div id="weixin-login-link" class="hint"></div>
      </div>
      <form method="post" action="/api/setup/weixin/unbind" onsubmit="return confirm('确认解绑当前本机 Weixin 状态？这会清除 HarborGate 本地保存的账号、context_token、polling 进度和运行状态。');">
        <button class="danger" type="submit"{unbind_disabled}>解绑当前微信状态</button>
        <span class="hint">解绑清除 HarborGate 本地 Weixin 状态；如果 systemd 环境变量仍固定账号，需要同步更新服务配置并重启 harborgate。</span>
      </form>
      <p class="hint">扫码成功后，HarborGate 会把账号凭据保存到本机 Weixin state dir；收消息由 <code>harborgate.service</code> 内部 runtime 长轮询。HarborDesk 会从 <code>/api/gateway/status</code> 读取这里的脱敏状态。</p>
    </div>
  </div>
  <script>
    const initialLogin = {login_state_json};
    const startButton = document.getElementById('weixin-login-start');
    const statusEl = document.getElementById('weixin-login-status');
    const qrEl = document.getElementById('weixin-login-qr');
    const linkEl = document.getElementById('weixin-login-link');
    let pollTimer = null;

    function renderLogin(data) {{
      const login = data.weixin_login || data || {{}};
      const status = login.status || 'not_started';
      const expires = Number(login.expires_in_seconds || 0);
      const suffix = expires > 0 && ['wait', 'scaned', 'scaned_but_redirect'].includes(status)
        ? `，剩余约 ${{expires}} 秒`
        : '';
      statusEl.className = status === 'error' || status === 'expired' ? 'hint err' : 'hint';
      statusEl.textContent = `扫码状态：${{status}}${{suffix}}${{login.last_error ? ' / ' + login.last_error : ''}}`;
      if (login.qrcode_available) {{
        qrEl.style.display = 'block';
        qrEl.src = `/setup/weixin/qr.svg?ts=${{Date.now()}}`;
        linkEl.textContent = login.qrcode_url || '';
      }} else {{
        qrEl.style.display = 'none';
        qrEl.removeAttribute('src');
        linkEl.textContent = '';
      }}
      if (status === 'confirmed') {{
        statusEl.className = 'hint ok';
        statusEl.textContent = `扫码登录完成：${{login.account_id_masked || 'Weixin'}}。页面即将刷新。`;
        window.setTimeout(() => window.location.reload(), 1200);
      }}
    }}

    async function pollLogin() {{
      window.clearTimeout(pollTimer);
      const response = await fetch('/api/setup/weixin/login/status');
      const data = await response.json();
      renderLogin(data);
      const status = (data.weixin_login || {{}}).status || '';
      if (['wait', 'scaned', 'scaned_but_redirect', 'error'].includes(status)) {{
        pollTimer = window.setTimeout(pollLogin, 2000);
      }}
    }}

    startButton.addEventListener('click', async () => {{
      startButton.disabled = true;
      statusEl.className = 'hint';
      statusEl.textContent = '正在向 Weixin 申请扫码二维码...';
      try {{
        const response = await fetch('/api/setup/weixin/login/start', {{ method: 'POST' }});
        const data = await response.json();
        renderLogin(data);
        if (!response.ok || !data.ok) {{
          throw new Error(data.message || '生成二维码失败');
        }}
        pollTimer = window.setTimeout(pollLogin, 1500);
      }} catch (error) {{
        statusEl.className = 'hint err';
        statusEl.textContent = error.message;
      }} finally {{
        startButton.disabled = false;
      }}
    }});

    if (initialLogin && initialLogin.qrcode_available && ['wait', 'scaned', 'scaned_but_redirect'].includes(initialLogin.status)) {{
      renderLogin(initialLogin);
      pollTimer = window.setTimeout(pollLogin, 1500);
    }}
  </script>
</body>
</html>"""

    def build_qr_page(self, *, request_host: str = "") -> str:
        status = self.build_status_payload(request_host=request_host)
        setup_url = _html_escape(str(status["feishu"].get("setup_url") or status["setup_url"]))
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HarborGate Setup QR</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f4efe7; color: #1e1b18; margin: 0; }}
    .wrap {{ max-width: 520px; margin: 0 auto; padding: 28px 18px 48px; text-align: center; }}
    .card {{ background: rgba(255,255,255,0.92); border-radius: 20px; padding: 20px; box-shadow: 0 18px 48px rgba(51,36,18,0.12); }}
    img {{ width: 280px; height: 280px; background: #f8f3ec; border-radius: 18px; }}
    code {{ word-break: break-all; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>手机扫码配置 Feishu</h1>
      <p>扫下面这个二维码，打开本机 HarborGate 的 Feishu 配置页。</p>
      <img src="/setup/feishu/qr.svg" alt="setup qr" />
      <p><code>{setup_url}</code></p>
    </div>
  </div>
</body>
</html>"""

    def build_qr_svg(self, *, request_host: str = "") -> str:
        status = self.build_status_payload(request_host=request_host)
        return _qr_to_svg(str(status["feishu"].get("setup_url") or status["setup_url"]))

    def configure_feishu(self, body: dict[str, Any], *, request_host: str = "") -> tuple[int, dict[str, Any]]:
        expected_session = self.store.current_session_code()
        session_code = str(body.get("session_code") or "").strip().upper()
        if session_code != expected_session:
            return 403, {
                "success": False,
                "message": "setup session code is missing or invalid",
            }

        app_id = str(body.get("app_id") or "").strip()
        app_secret = str(body.get("app_secret") or "").strip()
        verification_token = str(body.get("verification_token") or "").strip()
        if not app_id or not app_secret:
            return 422, {
                "success": False,
                "message": "app_id and app_secret are required",
            }

        adapter = self.ensure_feishu_adapter()
        settings = self._build_feishu_settings(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=verification_token,
            connection_mode="websocket",
            enable_live_send=True,
        )
        validator = FeishuAdapter(settings)
        try:
            bot_info = validator.fetch_bot_info()
        except Exception as exc:  # noqa: BLE001
            return 422, {
                "success": False,
                "message": f"Feishu validation failed: {exc}",
            }

        applied_settings = self._build_feishu_settings(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=verification_token,
            connection_mode="websocket",
            enable_live_send=True,
            app_name=str(bot_info.get("app_name") or "").strip(),
            bot_open_id=str(bot_info.get("open_id") or "").strip(),
            bot_user_id=str(bot_info.get("user_id") or "").strip(),
        )
        adapter.apply_settings(applied_settings)
        saved = self.store.save_feishu_state(
            {
                "app_id": app_id,
                "app_secret": app_secret,
                "verification_token": verification_token,
                "connection_mode": "websocket",
                "enable_live_send": True,
                "app_name": str(bot_info.get("app_name") or "").strip(),
                "tenant_key": str(bot_info.get("tenant_key") or "").strip(),
                "bot_open_id": str(bot_info.get("open_id") or "").strip(),
                "bot_user_id": str(bot_info.get("user_id") or "").strip(),
                "status": "validated",
                "last_validated_at": _now_utc(),
                "webhook_url": "",
            }
        )
        return 200, {
            "success": True,
            "message": "Feishu credentials validated and applied to the running gateway.",
            "connection_mode": "websocket",
            "transport_status": adapter.transport_status(),
            "bot_info": {
                "app_name": saved.get("app_name", ""),
                "tenant_key": saved.get("tenant_key", ""),
                "open_id": saved.get("bot_open_id", ""),
                "user_id": saved.get("bot_user_id", ""),
            },
            "next_steps": [
                "在飞书开放平台里把事件订阅方式切换为使用长连接接收事件。",
                "订阅 im.message.receive_v1 事件。",
                "发布应用版本后给机器人发一条私信测试。",
            ],
        }

    def resolve_public_origin(self, *, request_host: str = "") -> str:
        if self.public_origin:
            return self.public_origin
        if request_host:
            host = request_host.strip()
            if host and not self._looks_like_loopback_host(host):
                return f"http://{host}"
        bind_host = self.bind_host.strip() or "127.0.0.1"
        if bind_host not in {"0.0.0.0", "::", "127.0.0.1", "localhost"}:
            return f"http://{bind_host}:{self.bind_port}"
        if bind_host in {"0.0.0.0", "::"}:
            local_ip = self._discover_local_ip()
            if local_ip:
                return f"http://{local_ip}:{self.bind_port}"
        return f"http://127.0.0.1:{self.bind_port}"

    def build_setup_url(self, *, origin: str, session_code: str) -> str:
        return f"{origin}/setup?session={parse.quote(session_code)}"

    def build_feishu_setup_url(self, *, origin: str, session_code: str) -> str:
        return f"{origin}/setup/feishu?session={parse.quote(session_code)}"

    @staticmethod
    def _discover_local_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                return str(sock.getsockname()[0])
        except OSError:
            return ""

    @staticmethod
    def _looks_like_loopback_host(value: str) -> bool:
        host = value.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].strip().lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    @staticmethod
    def _is_mobile_reachable(origin: str) -> bool:
        return not SetupPortalService._looks_like_loopback_host(origin)

    def _build_feishu_settings(
        self,
        *,
        app_id: str,
        app_secret: str,
        verification_token: str,
        connection_mode: str,
        enable_live_send: bool,
        app_name: str = "",
        bot_open_id: str = "",
        bot_user_id: str = "",
    ) -> FeishuSettings:
        adapter = self.gateway.get_adapter("feishu")
        base = adapter.settings if isinstance(adapter, FeishuAdapter) else FeishuSettings(app_id="", app_secret="")
        return FeishuSettings(
            app_id=app_id,
            app_secret=app_secret,
            domain=base.domain,
            connection_mode=connection_mode,
            allowed_users=set(base.allowed_users),
            group_policy=base.group_policy,
            bot_open_id=bot_open_id,
            bot_user_id=bot_user_id,
            bot_name=app_name,
            verification_token=verification_token,
            encrypt_key=base.encrypt_key,
            webhook_host=base.webhook_host,
            webhook_port=base.webhook_port,
            webhook_path=base.webhook_path,
            base_url=base.base_url,
            auth_base_url=base.auth_base_url,
            enable_live_send=enable_live_send,
            timeout_seconds=base.timeout_seconds,
        )

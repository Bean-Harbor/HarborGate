from __future__ import annotations

import json
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

from im_agent.gateway import GatewayService
from im_agent.platforms.feishu import FeishuAdapter, FeishuSettings


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
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.public_origin = public_origin.strip().rstrip("/")

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

    def ensure_feishu_adapter(self) -> FeishuAdapter:
        adapter = self.gateway.get_adapter("feishu")
        if isinstance(adapter, FeishuAdapter):
            return adapter
        adapter = FeishuAdapter()
        self.gateway.register_adapter(adapter)
        return adapter

    def build_status_payload(self, *, request_host: str = "") -> dict[str, Any]:
        state = self.store.load_state()
        adapter = self.ensure_feishu_adapter()
        feishu_state = state.get("feishu") or {}
        if not isinstance(feishu_state, dict):
            feishu_state = {}

        origin = self.resolve_public_origin(request_host=request_host)
        session_code = str(state.get("session_code") or "")
        setup_url = self.build_setup_url(origin=origin, session_code=session_code)
        webhook_url = ""
        if adapter.settings.connection_mode == "webhook":
            webhook_url = parse.urljoin(f"{origin}/", adapter.webhook_path.lstrip("/"))
        transport = adapter.transport_status()

        current_app_id = str(feishu_state.get("app_id") or adapter.settings.app_id or "").strip()
        current_app_name = str(feishu_state.get("app_name") or adapter.settings.bot_name or "").strip()
        configured = bool(current_app_id and (feishu_state.get("app_secret") or adapter.settings.app_secret))
        connected = bool(transport.get("connected"))
        transport_status = str(transport.get("status") or ("waiting_for_credentials" if not configured else "ready")).strip()
        return {
            "session_code": session_code,
            "setup_url": setup_url,
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
            },
        }

    def build_setup_page(self, *, request_host: str = "") -> str:
        status = self.build_status_payload(request_host=request_host)
        feishu = status["feishu"]
        setup_url = _html_escape(str(status["setup_url"]))
        qr_path = _html_escape("/setup/qr.svg")
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
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>IM Gateway Feishu 配置</title>
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
      <div class="meta">IM Gateway · Feishu 手机配置页</div>
      <h1>扫码后直接填飞书凭证</h1>
      <p>这个页面会把 <code>app_id</code> 和 <code>app_secret</code> 保存在当前 IM Gateway 本机，并立即更新正在运行的 Feishu adapter，不需要用户手动登录到服务器。</p>
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

    def build_qr_page(self, *, request_host: str = "") -> str:
        status = self.build_status_payload(request_host=request_host)
        setup_url = _html_escape(str(status["setup_url"]))
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>IM Gateway Setup QR</title>
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
      <p>扫下面这个二维码，打开本机 IM Gateway 的 Feishu 配置页。</p>
      <img src="/setup/qr.svg" alt="setup qr" />
      <p><code>{setup_url}</code></p>
    </div>
  </div>
</body>
</html>"""

    def build_qr_svg(self, *, request_host: str = "") -> str:
        status = self.build_status_payload(request_host=request_host)
        return _qr_to_svg(str(status["setup_url"]))

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

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib import error, request

from im_agent.brain import RuleBasedBrain
from im_agent.gateway import GatewayService
from im_agent.platforms.feishu import FeishuAdapter, FeishuSettings
from im_agent.platforms.webhook import WebhookAdapter
from im_agent.platforms.weixin import (
    QRChallenge,
    WeixinAdapter,
    save_weixin_account,
    save_weixin_context_tokens,
    save_weixin_transport_state,
)
from im_agent.server import build_handler
from im_agent.session_store import FileSessionStore
from im_agent.setup_portal import FileSetupPortalStore, SetupPortalService


class FakeSetupFeishuHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/open-apis/bot/v3/info":
            payload = {
                "code": 0,
                "msg": "success",
                "data": {
                    "app_name": "Setup Bot",
                    "tenant_key": "tenant_setup_123",
                    "open_id": "ou_setup_bot",
                    "user_id": "bot_user_setup",
                },
            }
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/open-apis/auth/v3/tenant_access_token/internal":
            payload = {
                "code": 0,
                "msg": "success",
                "tenant_access_token": "tenant_token_setup",
                "expire": 7200,
            }
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class NotificationServerTests(unittest.TestCase):
    def test_notification_delivery_http_endpoint_returns_shared_success_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            gateway.register_adapter(WebhookAdapter())
            gateway.handle_inbound(
                "webhook",
                {
                    "platform": "feishu",
                    "chat_id": "room-1",
                    "user_id": "alice",
                    "text": "hello there",
                    "message_id": "msg-1",
                },
            )
            route_key = str(gateway.store.load_metadata("feishu", "room-1")["route_key"])
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = {
                    "notification_id": "notif-1",
                    "trace_id": "trace-1",
                    "destination": {
                        "kind": "conversation",
                        "route_key": route_key,
                    },
                    "content": {
                        "title": "Front Door",
                        "body": "Done",
                        "payload_format": "plain_text",
                        "structured_payload": {},
                        "attachments": [],
                    },
                    "delivery": {
                        "mode": "send",
                        "reply_to_message_id": "",
                        "update_message_id": "",
                        "idempotency_key": "idem-http-1",
                    },
                }
                body = json.dumps(payload).encode("utf-8")
                req = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/notifications/deliveries",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Contract-Version": "2.0",
                    },
                    method="POST",
                )
                with request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(data["ok"])
                self.assertEqual(data["status"], "sent")
                self.assertEqual(data["notification_id"], "notif-1")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_notification_delivery_http_endpoint_uses_shared_error_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            gateway.register_adapter(WebhookAdapter())
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = {
                    "notification_id": "notif-1",
                    "trace_id": "trace-1",
                    "destination": {
                        "kind": "conversation",
                        "route_key": "gw_route_missing",
                    },
                    "content": {
                        "title": "Front Door",
                        "body": "Done",
                        "payload_format": "plain_text",
                        "structured_payload": {},
                        "attachments": [],
                    },
                    "delivery": {
                        "mode": "send",
                        "reply_to_message_id": "",
                        "update_message_id": "",
                        "idempotency_key": "idem-http-2",
                    },
                }
                body = json.dumps(payload).encode("utf-8")
                req = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/notifications/deliveries",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Contract-Version": "2.0",
                    },
                    method="POST",
                )
                with self.assertRaises(error.HTTPError) as ctx:
                    request.urlopen(req, timeout=5)
                data = json.loads(ctx.exception.read().decode("utf-8"))

                self.assertEqual(ctx.exception.code, 404)
                self.assertFalse(data["ok"])
                self.assertEqual(data["error"]["code"], "ROUTE_NOT_FOUND")
                self.assertEqual(data["trace_id"], "trace-1")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_feishu_webhook_handles_url_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            gateway.register_adapter(
                FeishuAdapter(
                    FeishuSettings(
                        app_id="cli_xxx",
                        app_secret="secret_xxx",
                        connection_mode="webhook",
                        verification_token="verify_123",
                        webhook_path="/feishu/webhook",
                    )
                )
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps(
                    {
                        "type": "url_verification",
                        "token": "verify_123",
                        "challenge": "challenge_abc",
                    }
                ).encode("utf-8")
                req = request.Request(
                    f"http://127.0.0.1:{server.server_port}/feishu/webhook",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertEqual(data["challenge"], "challenge_abc")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_setup_status_endpoint_returns_mobile_setup_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            runtime_dir = Path(tmp) / "runtime"
            (runtime_dir / "weixin-ingress-probe").mkdir(parents=True, exist_ok=True)
            (runtime_dir / "platform-live-gate").mkdir(parents=True, exist_ok=True)
            (runtime_dir / "weixin-ingress-probe" / "probe-1.json").write_text(
                json.dumps(
                    {
                        "provider_private_text_seen": True,
                        "provider_private_text_count": 1,
                        "blocked_reason": "",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (runtime_dir / "platform-live-gate" / "gate-1.json").write_text(
                json.dumps(
                    {
                        "decision": "dual_surface_ready",
                        "decision_reason": "feishu_and_weixin_rehearsal_ready",
                        "parity_ready": True,
                        "weixin_blocker_category": "",
                        "release_v2": {
                            "delivery_policy": {
                                "interactive_reply": "source_bound",
                                "proactive_delivery": "user-default-configured",
                            }
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            weixin_state_dir = os.path.join(tmp, "weixin")
            save_weixin_account(
                weixin_state_dir,
                account_id="wx-account-1",
                token="wx-secret-1",
                base_url="https://ilinkai.weixin.qq.com",
                user_id="wx-user-1",
            )
            save_weixin_transport_state(
                weixin_state_dir,
                "wx-account-1",
                {
                    "mode": "polling",
                    "status": "polling_idle",
                    "connected": True,
                    "last_error": "",
                    "last_poll_outcome": "idle_timeout",
                    "last_poll_at": "2026-04-20T09:30:00Z",
                    "last_getupdates_at": "2026-04-20T09:30:00Z",
                    "last_getupdates_buf": "cursor-1",
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
                    "last_inbound_at": "",
                    "last_inbound_message_id": "",
                    "last_inbound_chat_id": "",
                },
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="0.0.0.0",
                bind_port=0,
                public_origin="http://192.168.3.10:8787",
                runtime_root=runtime_dir,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with request.urlopen(f"http://127.0.0.1:{server.server_port}/api/setup/status", timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertIn("setup_url", data)
                self.assertEqual(data["manage_url"], "http://192.168.3.10:8787/admin/im")
                self.assertEqual(data["static_setup_url"], "http://192.168.3.10:8787/setup")
                self.assertEqual(data["qr_page_url"], "http://192.168.3.10:8787/setup/qr")
                self.assertEqual(data["qr_svg_url"], "http://192.168.3.10:8787/setup/qr.svg")
                self.assertEqual(data["public_origin"], "http://192.168.3.10:8787")
                self.assertFalse(data["feishu"]["configured"])
                self.assertFalse(data["feishu"]["api_key_configured"])
                self.assertEqual(data["feishu"]["readiness"]["state"], "not_configured")
                self.assertTrue(data["weixin"]["configured"])
                self.assertEqual(data["weixin"]["qr_status"], "configured")
                self.assertEqual(data["weixin"]["manage_status"], "available")
                self.assertEqual(data["weixin"]["manage_url"], "http://192.168.3.10:8787/admin/im/weixin")
                self.assertEqual(data["weixin"]["setup_url"], "http://192.168.3.10:8787/setup/weixin")
                self.assertEqual(data["feishu"]["manage_url"], "http://192.168.3.10:8787/admin/im/feishu")
                self.assertEqual(
                    data["feishu"]["setup_url"],
                    f"http://192.168.3.10:8787/setup/feishu?session={setup_portal.store.current_session_code()}",
                )
                self.assertEqual(data["connectors"]["weixin"]["setup_url"], data["weixin"]["setup_url"])
                self.assertEqual(data["connectors"]["feishu"]["setup_url"], data["feishu"]["setup_url"])
                self.assertNotEqual(data["weixin"]["setup_url"], data["feishu"]["setup_url"])
                self.assertEqual(data["weixin"]["blocker_category"], "context_token_send")
                self.assertEqual(data["weixin"]["ingress_blocker_category"], "context_token_send")
                self.assertEqual(data["weixin"]["readiness"]["state"], "blocked")
                self.assertEqual(data["weixin"]["readiness"]["reason"], "context_token_send")
                self.assertTrue(data["weixin"]["connected"])
                self.assertEqual(data["weixin"]["status"], "polling_idle")
                self.assertIn("ingress_observability", data["weixin"])
                self.assertIn("delivery_observability", data["weixin"])
                self.assertEqual(data["weixin"]["poll"]["last_private_text_message_at"], "")
                self.assertEqual(data["weixin"]["ingress_observability"]["last_getupdates_count"], 0)
                self.assertEqual(data["weixin"]["ingress_observability"]["last_private_text_message_at"], "")
                self.assertEqual(data["weixin"]["delivery_observability"]["last_send_status"], "")
                self.assertIn("gateway_status", data)
                self.assertTrue(data["gateway_status"]["ok"])
                self.assertEqual(data["gateway_status"]["manage_url"], "http://192.168.3.10:8787/admin/im")
                self.assertEqual(data["gateway_status"]["static_setup_url"], "http://192.168.3.10:8787/setup")
                self.assertEqual(data["gateway_status"]["qr_page_url"], "http://192.168.3.10:8787/setup/qr")
                self.assertEqual(data["gateway_status"]["qr_svg_url"], "http://192.168.3.10:8787/setup/qr.svg")
                self.assertFalse(data["gateway_status"]["feishu"]["api_key_configured"])
                self.assertEqual(data["gateway_status"]["feishu"]["readiness"]["state"], "not_configured")
                self.assertEqual(data["gateway_status"]["weixin"]["blocker_category"], "context_token_send")
                self.assertEqual(data["gateway_status"]["weixin"]["ingress_blocker_category"], "context_token_send")
                self.assertEqual(data["gateway_status"]["weixin"]["qr_status"], "configured")
                self.assertEqual(data["gateway_status"]["weixin"]["manage_url"], "http://192.168.3.10:8787/admin/im/weixin")
                self.assertEqual(data["gateway_status"]["weixin"]["setup_url"], "http://192.168.3.10:8787/setup/weixin")
                self.assertEqual(data["gateway_status"]["weixin"]["readiness"]["reason"], "context_token_send")
                self.assertIn("weixin", [channel["platform"] for channel in data["gateway_status"]["channels"]])
                weixin_channel = next(
                    channel for channel in data["gateway_status"]["channels"] if channel["platform"] == "weixin"
                )
                self.assertTrue(weixin_channel["connected"])
                self.assertEqual(weixin_channel["transport"]["status"], "polling_idle")
                self.assertIn("delivery_policy", data)
                self.assertEqual(data["delivery_policy"]["interactive_reply"], "source_bound")
                self.assertIn("release_v2", data)
                self.assertEqual(data["release_v2"]["decision"], "dual_surface_ready")
                self.assertTrue(data["release_v2"]["weixin_ingress_proof"]["provider_private_text_seen"])
                self.assertEqual(
                    data["gateway_status"]["delivery_policy"]["proactive_delivery"],
                    "user-default-configured",
                )
                self.assertTrue(data["gateway_status"]["release_v2"]["dual_surface_ready"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_setup_status_uses_persisted_weixin_transport_state_when_runner_updates_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            weixin_state_dir = os.path.join(tmp, "weixin")
            save_weixin_account(
                weixin_state_dir,
                account_id="wx-account-runtime",
                token="wx-secret-runtime",
                base_url="https://ilinkai.weixin.qq.com",
                user_id="wx-user-runtime",
            )
            gateway.register_adapter(WeixinAdapter(state_dir=weixin_state_dir, account_id="wx-account-runtime"))
            save_weixin_transport_state(
                weixin_state_dir,
                "wx-account-runtime",
                {
                    "mode": "polling",
                    "status": "polling_idle",
                    "connected": True,
                    "last_error": "",
                    "last_poll_outcome": "empty",
                    "last_poll_at": "2026-04-20T10:00:00Z",
                    "last_getupdates_at": "2026-04-20T10:00:00Z",
                    "last_getupdates_buf": "cursor-runtime",
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
                    "last_inbound_at": "",
                    "last_inbound_message_id": "",
                    "last_inbound_chat_id": "",
                },
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
                weixin_state_dir=weixin_state_dir,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with request.urlopen(f"http://127.0.0.1:{server.server_port}/api/setup/status", timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(data["weixin"]["configured"])
                self.assertTrue(data["weixin"]["connected"])
                self.assertEqual(data["weixin"]["status"], "polling_idle")
                self.assertEqual(data["weixin"]["blocker_category"], "weixin_waiting_for_private_text")
                self.assertEqual(data["weixin"]["ingress_blocker_category"], "getupdates")
                self.assertEqual(
                    data["weixin"]["ingress_observability"]["blocked_reason"],
                    "waiting_for_private_text",
                )
                self.assertEqual(data["weixin"]["poll"]["last_private_text_message_at"], "")
                self.assertEqual(data["weixin"]["ingress_observability"]["last_private_text_message_at"], "")
                self.assertEqual(data["gateway_status"]["weixin"]["blocker_category"], "weixin_waiting_for_private_text")
                self.assertEqual(data["gateway_status"]["weixin"]["ingress_blocker_category"], "getupdates")
                weixin_channel = next(
                    channel for channel in data["gateway_status"]["channels"] if channel["platform"] == "weixin"
                )
                self.assertTrue(weixin_channel["connected"])
                self.assertEqual(weixin_channel["transport"]["status"], "polling_idle")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_setup_status_reads_context_tokens_from_safe_slugged_account_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            weixin_state_dir = os.path.join(tmp, "weixin")
            account_id = "d5ba3cf20a24@im.bot"
            save_weixin_account(
                weixin_state_dir,
                account_id=account_id,
                token="wx-secret-d5ba",
                base_url="https://ilinkai.weixin.qq.com",
                user_id="wx-user-d5ba",
            )
            save_weixin_context_tokens(
                weixin_state_dir,
                account_id,
                {"wx-chat-1": "ctx-d5ba"},
            )
            save_weixin_transport_state(
                weixin_state_dir,
                account_id,
                {
                    "mode": "polling",
                    "status": "polling_idle",
                    "connected": True,
                    "last_error": "",
                    "last_poll_outcome": "idle_timeout",
                    "last_poll_at": "2026-04-25T09:00:00Z",
                    "last_getupdates_at": "2026-04-25T09:00:00Z",
                    "last_getupdates_buf": "cursor-d5ba",
                    "last_getupdates_count": 0,
                    "last_private_text_message_count": 0,
                    "last_private_text_message_at": "2026-04-25T09:00:10Z",
                    "last_getupdates_message_ids": [],
                    "last_getupdates_private_message_ids": [],
                    "last_getupdates_error": "",
                    "last_context_token_at": "2026-04-25T09:00:10Z",
                    "last_send_at": "2026-04-25T09:00:11Z",
                    "last_send_chunk_count": 1,
                    "last_send_status": "sent",
                    "last_send_error": "",
                    "last_send_retryable": False,
                    "last_send_provider_message_id": "provider-d5ba",
                    "last_send_context_token_used": True,
                    "last_send_attachment_count": 0,
                    "last_send_content_kind": "text",
                    "last_inbound_at": "2026-04-25T09:00:10Z",
                    "last_inbound_message_id": "wx-msg-d5ba",
                    "last_inbound_chat_id": "wx-chat-1",
                },
            )
            gateway.register_adapter(WeixinAdapter(state_dir=weixin_state_dir, account_id=account_id))
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
                weixin_state_dir=weixin_state_dir,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with request.urlopen(f"http://127.0.0.1:{server.server_port}/api/setup/status", timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertEqual(data["weixin"]["context_token_count"], 1)
                self.assertEqual(data["weixin"]["blocker_category"], "")
                self.assertEqual(data["weixin"]["ingress_blocker_category"], "")
                self.assertTrue(data["gateway_status"]["weixin"]["connected"])
                self.assertEqual(data["gateway_status"]["weixin"]["blocker_category"], "")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_setup_and_gateway_status_redact_configured_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            runtime_dir = Path(tmp) / "runtime"
            (runtime_dir / "weixin-ingress-probe").mkdir(parents=True, exist_ok=True)
            (runtime_dir / "weixin-ingress-probe" / "probe-redact.json").write_text(
                json.dumps(
                    {
                        "provider_private_text_seen": False,
                        "provider_private_text_count": 0,
                        "blocked_reason": "token=wx-token-redact context_token=ctx-token-redact",
                        "token": "wx-token-redact",
                        "context_token": "ctx-token-redact",
                        "bot_token": "bot-token-redact",
                        "nested": {
                            "api_key": "feishu-api-key-redact",
                            "message": "Bearer feishu-bearer-redact",
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            weixin_state_dir = os.path.join(tmp, "weixin")
            save_weixin_account(
                weixin_state_dir,
                account_id="wx-account-redact",
                token="wx-token-redact",
                base_url="https://ilinkai.weixin.qq.com",
                user_id="wx-user-redact",
            )
            save_weixin_context_tokens(
                weixin_state_dir,
                "wx-account-redact",
                {"wx-chat-redact": "ctx-token-redact"},
            )
            save_weixin_transport_state(
                weixin_state_dir,
                "wx-account-redact",
                {
                    "mode": "polling",
                    "status": "error",
                    "connected": False,
                    "last_error": "token wx-token-redact context ctx-token-redact bot_token=bot-token-redact",
                    "last_poll_outcome": "error",
                    "last_poll_at": "2026-04-25T08:30:00Z",
                    "last_getupdates_at": "2026-04-25T08:30:00Z",
                    "last_getupdates_buf": "cursor-redact",
                    "last_getupdates_count": 0,
                    "last_private_text_message_count": 0,
                    "last_private_text_message_at": "",
                    "last_getupdates_message_ids": [],
                    "last_getupdates_private_message_ids": [],
                    "last_getupdates_error": "getupdates failed with ctx-token-redact",
                    "last_context_token_at": "2026-04-25T08:30:10Z",
                    "last_send_at": "",
                    "last_send_chunk_count": 0,
                    "last_send_status": "",
                    "last_send_error": "context_token=ctx-token-redact token=wx-token-redact",
                    "last_send_retryable": False,
                    "last_send_provider_message_id": "",
                    "last_send_context_token_used": False,
                    "last_inbound_at": "",
                    "last_inbound_message_id": "",
                    "last_inbound_chat_id": "",
                },
            )
            gateway.register_adapter(WeixinAdapter(state_dir=weixin_state_dir, account_id="wx-account-redact"))

            feishu_adapter = FeishuAdapter(
                FeishuSettings(
                    app_id="cli_redact_123",
                    app_secret="secret-redact-456",
                    verification_token="verify-redact-789",
                    bot_name="Redact Bot",
                    enable_live_send=True,
                )
            )
            feishu_adapter._set_transport_state(  # type: ignore[attr-defined]
                status="error",
                connected=False,
                last_error=(
                    "app_secret=secret-redact-456 api_key=feishu-api-key-redact "
                    "bot_token=bot-token-redact authorization=Bearer feishu-bearer-redact"
                ),
            )
            gateway.register_adapter(feishu_adapter)

            store = FileSetupPortalStore(tmp)
            store.save_feishu_state(
                {
                    "app_id": "cli_redact_123",
                    "app_secret": "secret-redact-456",
                    "verification_token": "verify-redact-789",
                    "connection_mode": "websocket",
                    "enable_live_send": True,
                    "app_name": "Redact Bot",
                    "status": "validated",
                }
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=store,
                bind_host="127.0.0.1",
                bind_port=0,
                weixin_state_dir=weixin_state_dir,
                runtime_root=runtime_dir,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with request.urlopen(f"http://127.0.0.1:{server.server_port}/api/setup/status", timeout=5) as response:
                    setup_data = json.loads(response.read().decode("utf-8"))

                with patch.dict(os.environ, {"IM_AGENT_SERVICE_TOKEN": "status-secret"}, clear=False):
                    req = request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/gateway/status",
                        headers={
                            "X-Contract-Version": "2.0",
                            "Authorization": "Bearer status-secret",
                        },
                        method="GET",
                    )
                    with request.urlopen(req, timeout=5) as response:
                        gateway_data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(setup_data["feishu"]["api_key_configured"])
                self.assertTrue(gateway_data["feishu"]["api_key_configured"])
                self.assertEqual(setup_data["weixin"]["qr_status"], "configured")
                self.assertEqual(gateway_data["weixin"]["qr_status"], "configured")
                self.assertEqual(setup_data["weixin"]["manage_status"], "available")
                self.assertEqual(gateway_data["weixin"]["manage_status"], "available")
                self.assertEqual(setup_data["weixin"]["ingress_proof"]["context_token"], "[REDACTED]")
                self.assertEqual(setup_data["release_v2"]["weixin_ingress_proof"]["token"], "[REDACTED]")
                self.assertEqual(gateway_data["release_v2"]["weixin_ingress_proof"]["bot_token"], "[REDACTED]")
                feishu_channel = next(item for item in gateway_data["channels"] if item["platform"] == "feishu")
                self.assertIn("api_key=[REDACTED]", feishu_channel["transport"]["last_error"])

                for payload in (setup_data, gateway_data):
                    response_text = json.dumps(payload, ensure_ascii=False)
                    self.assertIn("[REDACTED]", response_text)
                    for secret in (
                        "secret-redact-456",
                        "verify-redact-789",
                        "feishu-api-key-redact",
                        "feishu-bearer-redact",
                        "bot-token-redact",
                        "wx-token-redact",
                        "ctx-token-redact",
                        "cursor-redact",
                    ):
                        self.assertNotIn(secret, response_text)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_weixin_qr_status_projection_states_are_stable(self) -> None:
        self.assertEqual(SetupPortalService._weixin_qr_status({"configured": False}), "login_required")
        self.assertEqual(
            SetupPortalService._weixin_qr_status(
                {"configured": True, "ingress_blocker_category": "account_restore"}
            ),
            "login_required",
        )
        self.assertEqual(
            SetupPortalService._weixin_qr_status({"configured": True, "ingress_blocker_category": "qr_recovery"}),
            "recovery_required",
        )
        self.assertEqual(
            SetupPortalService._weixin_qr_status(
                {"configured": True, "blocker_category": "weixin_provider_auth_failed"}
            ),
            "recovery_required",
        )
        for blocker in ("", "getupdates", "context_token_send", "weixin_dns_resolution"):
            self.assertEqual(
                SetupPortalService._weixin_qr_status({"configured": True, "ingress_blocker_category": blocker}),
                "configured",
            )

    def test_setup_qr_svg_endpoint_returns_svg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="0.0.0.0",
                bind_port=0,
                public_origin="http://192.168.3.10:8787",
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with request.urlopen(f"http://127.0.0.1:{server.server_port}/setup/qr.svg", timeout=5) as response:
                    svg = response.read().decode("utf-8")

                self.assertIn("<svg", svg)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_setup_feishu_configure_endpoint_validates_and_applies_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="0.0.0.0",
                bind_port=0,
                public_origin="http://192.168.3.10:8787",
            )
            session_code = setup_portal.store.current_session_code()

            feishu_api_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSetupFeishuHandler)
            feishu_thread = threading.Thread(target=feishu_api_server.serve_forever, daemon=True)
            feishu_thread.start()

            adapter = setup_portal.ensure_feishu_adapter()
            base_url = f"http://127.0.0.1:{feishu_api_server.server_port}"
            adapter.apply_settings(
                FeishuSettings(
                    app_id="",
                    app_secret="",
                    base_url=base_url,
                    auth_base_url=base_url,
                    webhook_path="/feishu/webhook",
                )
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps(
                    {
                        "session_code": session_code,
                        "app_id": "cli_xxx",
                        "app_secret": "secret_xxx",
                        "verification_token": "verify_123",
                    }
                ).encode("utf-8")
                req = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/setup/feishu/configure",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(data["success"])
                configured_adapter = gateway.get_adapter("feishu")
                self.assertIsInstance(configured_adapter, FeishuAdapter)
                assert isinstance(configured_adapter, FeishuAdapter)
                self.assertEqual(configured_adapter.settings.app_id, "cli_xxx")
                self.assertEqual(configured_adapter.settings.bot_name, "Setup Bot")
                self.assertEqual(configured_adapter.settings.connection_mode, "websocket")
                self.assertTrue(configured_adapter.settings.enable_live_send)
                self.assertEqual(data["connection_mode"], "websocket")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                feishu_api_server.shutdown()
                feishu_api_server.server_close()
                feishu_thread.join(timeout=2)

    def test_gateway_status_endpoint_returns_redacted_channel_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            save_weixin_account(
                tmp,
                account_id="wx-account-2",
                token="wx-secret-2",
                base_url="https://ilinkai.weixin.qq.com",
                user_id="wx-user-2",
            )
            save_weixin_transport_state(
                tmp,
                "wx-account-2",
                {
                    "mode": "polling",
                    "status": "error",
                    "connected": False,
                    "last_error": (
                        "HTTPSConnectionPool(host='ilinkai.weixin.qq.com'): "
                        "NameResolutionError token=wx-secret-2 account=wx-account-2"
                    ),
                    "last_poll_outcome": "error",
                    "last_poll_at": "2026-04-25T08:30:00Z",
                    "last_getupdates_at": "2026-04-25T08:30:00Z",
                    "last_getupdates_buf": "cursor-status",
                    "last_getupdates_count": 0,
                    "last_private_text_message_count": 0,
                    "last_private_text_message_at": "",
                    "last_getupdates_message_ids": [],
                    "last_getupdates_private_message_ids": [],
                    "last_getupdates_error": "<urlopen error [Errno 11001] getaddrinfo failed wx-secret-2>",
                    "last_context_token_at": "",
                    "last_send_at": "",
                    "last_send_chunk_count": 0,
                    "last_send_status": "",
                    "last_send_error": "send failed with token wx-secret-2",
                    "last_send_retryable": False,
                    "last_send_provider_message_id": "",
                    "last_send_context_token_used": False,
                    "last_inbound_at": "",
                    "last_inbound_message_id": "",
                    "last_inbound_chat_id": "",
                },
            )
            gateway.register_adapter(WeixinAdapter(state_dir=tmp, account_id="wx-account-2"))
            gateway.register_adapter(
                FeishuAdapter(
                    FeishuSettings(
                        app_id="cli_status_123",
                        app_secret="secret_status_456",
                        bot_name="Status Bot",
                        enable_live_send=True,
                    )
                )
            )
            feishu_adapter = gateway.get_adapter("feishu")
            if feishu_adapter is not None:
                feishu_adapter._set_transport_state(  # type: ignore[attr-defined]
                    status="error",
                    connected=False,
                    last_error="app_secret=secret_status_456 authorization=Bearer token_status_789",
                )
            if isinstance(feishu_adapter, FeishuAdapter):
                fake_delivery_response = {
                    "platform": "feishu",
                    "chat_id": "room-status",
                    "text": "status reply",
                    "timestamp": "2026-04-19T00:00:00Z",
                    "sent": True,
                    "message_id": "provider_msg_status",
                    "provider_message_id": "provider_msg_status",
                }
                with patch.object(feishu_adapter, "send_outbound", return_value=fake_delivery_response):
                    gateway.handle_inbound(
                        "feishu",
                        {
                            "chat_id": "room-status",
                            "user_id": "ou_status_user",
                            "text": "hello status",
                            "message_id": "msg-status-1",
                            "chat_type": "p2p",
                        },
                    )
                    route_key = str(gateway.store.load_metadata("feishu", "room-status")["route_key"])
                    gateway.handle_notification_delivery(
                        {
                            "notification_id": "notif-status-1",
                            "trace_id": "trace-status-1",
                            "destination": {
                                "kind": "conversation",
                                "route_key": route_key,
                            },
                            "content": {
                                "title": "Status",
                                "body": "source bound",
                                "payload_format": "plain_text",
                                "structured_payload": {},
                                "attachments": [],
                            },
                            "delivery": {
                                "mode": "send",
                                "reply_to_message_id": "",
                                "update_message_id": "",
                                "idempotency_key": "idem-status-source",
                            },
                        }
                    )
                    gateway.handle_notification_delivery(
                        {
                            "notification_id": "notif-status-2",
                            "trace_id": "trace-status-2",
                            "destination": {
                                "kind": "conversation",
                                "platform": "feishu",
                                "recipient": {
                                    "recipient_id": "oc_status_user",
                                    "recipient_type": "open_id",
                                },
                            },
                            "content": {
                                "title": "Status",
                                "body": "proactive",
                                "payload_format": "plain_text",
                                "structured_payload": {},
                                "attachments": [],
                            },
                            "delivery": {
                                "mode": "send",
                                "reply_to_message_id": "",
                                "update_message_id": "",
                                "idempotency_key": "idem-status-proactive",
                            },
                        }
                    )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
                weixin_state_dir=tmp,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"IM_AGENT_SERVICE_TOKEN": "status-secret"}, clear=False):
                    req = request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/gateway/status",
                        headers={
                            "X-Contract-Version": "2.0",
                            "Authorization": "Bearer status-secret",
                        },
                        method="GET",
                    )
                    with request.urlopen(req, timeout=5) as response:
                        data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(data["ok"])
                self.assertEqual(data["gateway_version"], "0.1.0")
                self.assertEqual(
                    data["gateway_base_url"],
                    f"http://127.0.0.1:{server.server_port}",
                )
                self.assertEqual(
                    data["manage_url"],
                    f"http://127.0.0.1:{server.server_port}/admin/im",
                )
                self.assertEqual(
                    data["setup_url"],
                    f"http://127.0.0.1:{server.server_port}/setup?session={setup_portal.store.current_session_code()}",
                )
                self.assertEqual(
                    data["static_setup_url"],
                    f"http://127.0.0.1:{server.server_port}/setup",
                )
                self.assertEqual(
                    data["qr_page_url"],
                    f"http://127.0.0.1:{server.server_port}/setup/qr",
                )
                self.assertEqual(
                    data["qr_svg_url"],
                    f"http://127.0.0.1:{server.server_port}/setup/qr.svg",
                )
                self.assertEqual(
                    data["connectors"]["feishu"]["setup_url"],
                    f"http://127.0.0.1:{server.server_port}/setup/feishu?session={setup_portal.store.current_session_code()}",
                )
                self.assertEqual(
                    data["connectors"]["weixin"]["setup_url"],
                    f"http://127.0.0.1:{server.server_port}/setup/weixin",
                )
                self.assertEqual(len(data["channels"]), 2)
                channel = next(item for item in data["channels"] if item["platform"] == "feishu")
                self.assertEqual(channel["platform"], "feishu")
                self.assertTrue(channel["enabled"])
                self.assertFalse(channel["connected"])
                self.assertEqual(channel["display_name"], "Status Bot")
                self.assertTrue(data["feishu"]["configured"])
                self.assertTrue(data["feishu"]["api_key_configured"])
                self.assertEqual(data["feishu"]["display_name"], "Status Bot")
                self.assertEqual(data["feishu"]["credential_status"], "validated")
                self.assertEqual(data["feishu"]["readiness"]["state"], "blocked")
                self.assertEqual(data["feishu"]["readiness"]["status"], "error")
                self.assertEqual(
                    data["feishu"]["manage_url"],
                    f"http://127.0.0.1:{server.server_port}/admin/im/feishu",
                )
                self.assertEqual(
                    data["feishu"]["setup_url"],
                    f"http://127.0.0.1:{server.server_port}/setup/feishu?session={setup_portal.store.current_session_code()}",
                )
                self.assertEqual(channel["manage_url"], data["feishu"]["manage_url"])
                self.assertTrue(channel["capabilities"]["reply"])
                self.assertFalse(channel["capabilities"]["update"])
                self.assertFalse(channel["capabilities"]["attachments"])
                self.assertEqual(channel["transport"]["status"], "error")
                self.assertIn("[REDACTED]", channel["transport"]["last_error"])
                weixin_channel = next(item for item in data["channels"] if item["platform"] == "weixin")
                self.assertEqual(weixin_channel["display_name"], "Weixin")
                self.assertEqual(weixin_channel["transport"]["status"], "error")
                self.assertFalse(weixin_channel["connected"])
                self.assertEqual(data["weixin"]["blocker_category"], "weixin_dns_resolution")
                self.assertEqual(data["weixin"]["ingress_blocker_category"], "getupdates")
                self.assertEqual(data["weixin"]["qr_status"], "configured")
                self.assertEqual(data["weixin"]["manage_status"], "available")
                self.assertEqual(
                    data["weixin"]["manage_url"],
                    f"http://127.0.0.1:{server.server_port}/admin/im/weixin",
                )
                self.assertEqual(
                    data["weixin"]["setup_url"],
                    f"http://127.0.0.1:{server.server_port}/setup/weixin",
                )
                self.assertNotEqual(data["weixin"]["setup_url"], data["feishu"]["setup_url"])
                self.assertEqual(weixin_channel["manage_url"], data["weixin"]["manage_url"])
                self.assertFalse(data["weixin"]["readiness"]["ready"])
                self.assertEqual(data["weixin"]["readiness"]["state"], "blocked")
                self.assertEqual(data["weixin"]["readiness"]["reason"], "getupdates")
                self.assertEqual(data["weixin"]["poll"]["status"], "error")
                self.assertEqual(data["weixin"]["poll"]["last_getupdates_buf"], "[REDACTED]")
                self.assertEqual(data["weixin"]["poll"]["last_private_text_message_at"], "")
                self.assertIn("getaddrinfo failed", data["weixin"]["poll"]["error"])
                self.assertIn("[REDACTED]", data["weixin"]["poll"]["error"])
                delivery_observability = data["delivery_observability"]
                self.assertEqual(delivery_observability["record_count"], 2)
                self.assertEqual(delivery_observability["source_bound_count"], 1)
                self.assertEqual(delivery_observability["proactive_count"], 1)
                self.assertEqual(delivery_observability["sent_count"], 2)
                self.assertEqual(delivery_observability["queue_state_counts"]["complete"], 2)
                self.assertEqual(delivery_observability["route_mode_counts"]["source_bound"], 1)
                self.assertEqual(delivery_observability["route_mode_counts"]["proactive"], 1)
                self.assertEqual(
                    delivery_observability["route_mode_breakdown"]["source_bound"]["queue_state_counts"]["complete"],
                    1,
                )
                self.assertEqual(
                    delivery_observability["route_mode_breakdown"]["proactive"]["queue_state_counts"]["complete"],
                    1,
                )
                self.assertEqual(
                    delivery_observability["route_mode_breakdown"]["source_bound"]["failure_class_counts"],
                    {},
                )
                self.assertEqual(
                    delivery_observability["route_mode_breakdown"]["proactive"]["failure_class_counts"],
                    {},
                )
                response_text = json.dumps(data, ensure_ascii=False)
                self.assertNotIn("secret_status_456", response_text)
                self.assertNotIn("cli_status_123", response_text)
                self.assertNotIn("wx-secret-2", response_text)
                self.assertNotIn("wx-account-2", response_text)
                self.assertNotIn("cursor-status", response_text)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_admin_im_alias_renders_setup_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/admin/im",
                    timeout=5,
                ) as response:
                    body = response.read().decode("utf-8")

                self.assertEqual(response.status, 200)
                self.assertIn("Feishu 手机配置页", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_admin_im_platform_dispatch_renders_weixin_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/admin/im/weixin",
                    timeout=5,
                ) as response:
                    body = response.read().decode("utf-8")

                self.assertEqual(response.status, 200)
                self.assertIn("HarborGate Weixin 配置", body)
                self.assertIn("微信配置与登录状态", body)
                self.assertIn("/api/setup/weixin/unbind", body)
                self.assertNotIn("Feishu 手机配置页", body)
                self.assertNotIn("扫码后直接填飞书凭证", body)

                with request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/admin/im?platform=weixin",
                    timeout=5,
                ) as response:
                    query_body = response.read().decode("utf-8")

                self.assertEqual(response.status, 200)
                self.assertIn("HarborGate Weixin 配置", query_body)
                self.assertNotIn("Feishu 手机配置页", query_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_weixin_setup_page_marks_configured_account_as_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            weixin_state_dir = Path(tmp) / "weixin"
            save_weixin_account(
                weixin_state_dir,
                account_id="wx-bound-1",
                token="wx-token-bound",
                base_url="https://ilinkai.weixin.qq.com",
                user_id="wx-user-bound",
            )
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
                weixin_state_dir=weixin_state_dir,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"WEIXIN_ACCOUNT_ID": ""}, clear=False):
                    with request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/setup/weixin",
                        timeout=5,
                    ) as response:
                        body = response.read().decode("utf-8")

                self.assertEqual(response.status, 200)
                self.assertIn("已绑定", body)
                self.assertIn("重新生成微信扫码登录二维码", body)
                self.assertIn("HarborGate 已保存本机 Weixin 账号状态", body)
                self.assertNotIn("如果状态是 login_required，请点击按钮生成二维码。", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_admin_weixin_unbound_query_redirects_to_setup_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                opener = request.build_opener(NoRedirectHandler)
                with self.assertRaises(error.HTTPError) as ctx:
                    opener.open(
                        f"http://127.0.0.1:{server.server_port}/admin/im/weixin?unbound=1",
                        timeout=5,
                    )

                self.assertEqual(ctx.exception.code, 303)
                self.assertEqual(ctx.exception.headers["Location"], "/setup/weixin?unbound=1")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_weixin_unbind_clears_saved_state_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            weixin_state_dir = Path(tmp) / "weixin"
            save_weixin_account(
                weixin_state_dir,
                account_id="wx-unbind-1",
                token="wx-token-unbind",
                base_url="https://ilinkai.weixin.qq.com",
                user_id="wx-user-unbind",
            )
            save_weixin_context_tokens(weixin_state_dir, "wx-unbind-1", {"chat-1": "ctx-unbind"})
            save_weixin_transport_state(
                weixin_state_dir,
                "wx-unbind-1",
                {
                    "mode": "polling",
                    "status": "polling_idle",
                    "connected": True,
                    "last_poll_outcome": "empty",
                },
            )
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            gateway.register_adapter(
                WeixinAdapter(
                    state_dir=weixin_state_dir,
                    account_id="wx-unbind-1",
                    token="wx-token-unbind",
                )
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
                weixin_state_dir=weixin_state_dir,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"WEIXIN_ACCOUNT_ID": ""}, clear=False):
                    req = request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/setup/weixin/unbind",
                        data=b"{}",
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                        method="POST",
                    )
                    with request.urlopen(req, timeout=5) as response:
                        payload = json.loads(response.read().decode("utf-8"))

                    self.assertEqual(response.status, 200)
                    self.assertTrue(payload["ok"])
                    self.assertFalse(payload["configured"])
                    self.assertFalse((weixin_state_dir / "accounts" / "wx-unbind-1.json").exists())
                    self.assertFalse((weixin_state_dir / "accounts" / "wx-unbind-1.context_tokens.json").exists())
                    self.assertFalse((weixin_state_dir / "accounts" / "wx-unbind-1.runtime.json").exists())

                    with request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/api/setup/status",
                        timeout=5,
                    ) as response:
                        status_payload = json.loads(response.read().decode("utf-8"))

                    self.assertFalse(status_payload["weixin"]["configured"])
                    self.assertEqual(status_payload["weixin"]["status"], "waiting_for_credentials")

                    save_weixin_account(
                        weixin_state_dir,
                        account_id="wx-unbind-2",
                        token="wx-token-unbind-2",
                        base_url="https://ilinkai.weixin.qq.com",
                        user_id="wx-user-unbind-2",
                    )
                    req = request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/setup/weixin/unbind",
                        data=b"",
                        headers={"Accept": "text/html"},
                        method="POST",
                    )
                    opener = request.build_opener(NoRedirectHandler)
                    with self.assertRaises(error.HTTPError) as ctx:
                        opener.open(req, timeout=5)

                    self.assertEqual(ctx.exception.code, 303)
                    self.assertEqual(ctx.exception.headers["Location"], "/setup/weixin?unbound=1")

                    with request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/setup/weixin?unbound=1",
                        timeout=5,
                    ) as response:
                        body = response.read().decode("utf-8")

                    self.assertEqual(response.status, 200)
                    self.assertIn("已解绑", body)
                    self.assertIn("生成微信扫码登录二维码", body)
                    self.assertNotIn("Feishu 手机配置页", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_weixin_setup_login_start_returns_browser_qr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
                weixin_state_dir=Path(tmp) / "weixin",
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("im_agent.setup_portal.request_weixin_qr_challenge") as qr_mock:
                    qr_mock.return_value = QRChallenge(
                        qrcode="qr-browser-start",
                        qrcode_img_content="https://liteapp.weixin.qq.com/q/example?qrcode=qr-browser-start",
                    )
                    req = request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/setup/weixin/login/start",
                        data=b"",
                        method="POST",
                    )
                    with request.urlopen(req, timeout=5) as response:
                        data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(data["ok"])
                self.assertEqual(data["weixin_login"]["status"], "wait")
                self.assertTrue(data["weixin_login"]["qrcode_available"])
                self.assertIn("liteapp.weixin.qq.com", data["weixin_login"]["qrcode_url"])

                with request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/setup/weixin/qr.svg",
                    timeout=5,
                ) as response:
                    svg = response.read().decode("utf-8")

                self.assertIn("<svg", svg)
                self.assertNotIn("bot_token", json.dumps(data, ensure_ascii=False))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_weixin_setup_login_confirm_saves_account_without_leaking_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            weixin_state_dir = Path(tmp) / "weixin"
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
                weixin_state_dir=weixin_state_dir,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("im_agent.setup_portal.request_weixin_qr_challenge") as qr_mock:
                    qr_mock.return_value = QRChallenge(
                        qrcode="qr-confirmed",
                        qrcode_img_content="https://liteapp.weixin.qq.com/q/example?qrcode=qr-confirmed",
                    )
                    req = request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/setup/weixin/login/start",
                        data=b"",
                        method="POST",
                    )
                    with request.urlopen(req, timeout=5):
                        pass

                with patch("im_agent.setup_portal.get_json") as status_mock:
                    status_mock.return_value = {
                        "status": "confirmed",
                        "ilink_bot_id": "wx-confirmed-account",
                        "bot_token": "wx-confirmed-secret-token",
                        "baseurl": "https://ilinkai.weixin.qq.com",
                        "ilink_user_id": "wx-confirmed-user",
                    }
                    with request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/api/setup/weixin/login/status",
                        timeout=5,
                    ) as response:
                        data = json.loads(response.read().decode("utf-8"))

                response_text = json.dumps(data, ensure_ascii=False)
                self.assertTrue(data["ok"])
                self.assertEqual(data["weixin_login"]["status"], "confirmed")
                self.assertNotIn("wx-confirmed-secret-token", response_text)
                self.assertTrue((weixin_state_dir / "accounts" / "wx-confirmed-account.json").exists())
                adapter = gateway.get_adapter("weixin")
                self.assertIsInstance(adapter, WeixinAdapter)
                self.assertTrue(adapter.configured)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_setup_platform_dispatch_keeps_feishu_and_weixin_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/setup/feishu",
                    timeout=5,
                ) as response:
                    feishu_body = response.read().decode("utf-8")
                with request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/setup/weixin",
                    timeout=5,
                ) as response:
                    weixin_body = response.read().decode("utf-8")

                self.assertIn("Feishu 手机配置页", feishu_body)
                self.assertIn("微信配置与登录状态", weixin_body)
                self.assertNotIn("Feishu 手机配置页", weixin_body)
                self.assertNotIn("扫码后直接填飞书凭证", weixin_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_gateway_status_endpoint_requires_service_auth_when_token_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"IM_AGENT_SERVICE_TOKEN": "status-secret"}, clear=False):
                    req = request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/gateway/status",
                        headers={"X-Contract-Version": "2.0"},
                        method="GET",
                    )
                    with self.assertRaises(error.HTTPError) as ctx:
                        request.urlopen(req, timeout=5)
                    data = json.loads(ctx.exception.read().decode("utf-8"))

                self.assertEqual(ctx.exception.code, 401)
                self.assertFalse(data["ok"])
                self.assertEqual(data["error"]["code"], "SERVICE_AUTH_FAILED")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_gateway_status_endpoint_never_uses_app_id_as_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            gateway.register_adapter(
                FeishuAdapter(
                    FeishuSettings(
                        app_id="cli_status_123",
                        app_secret="secret_status_456",
                        bot_name="",
                        enable_live_send=True,
                    )
                )
            )
            setup_portal = SetupPortalService(
                gateway=gateway,
                store=FileSetupPortalStore(tmp),
                bind_host="127.0.0.1",
                bind_port=0,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(gateway, setup_portal))
            setup_portal.bind_port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"IM_AGENT_SERVICE_TOKEN": "status-secret"}, clear=False):
                    req = request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/gateway/status",
                        headers={
                            "X-Contract-Version": "2.0",
                            "Authorization": "Bearer status-secret",
                        },
                        method="GET",
                    )
                    with request.urlopen(req, timeout=5) as response:
                        data = json.loads(response.read().decode("utf-8"))

                channel = data["channels"][0]
                self.assertEqual(channel["display_name"], "Feishu")
                response_text = json.dumps(data, ensure_ascii=False)
                self.assertNotIn("cli_status_123", response_text)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

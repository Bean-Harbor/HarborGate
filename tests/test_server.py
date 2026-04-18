import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request

from im_agent.brain import RuleBasedBrain
from im_agent.gateway import GatewayService
from im_agent.platforms.feishu import FeishuAdapter, FeishuSettings
from im_agent.platforms.webhook import WebhookAdapter
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
                        "X-Contract-Version": "1.5",
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
                        "X-Contract-Version": "1.5",
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
                with request.urlopen(f"http://127.0.0.1:{server.server_port}/api/setup/status", timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertIn("setup_url", data)
                self.assertEqual(data["public_origin"], "http://192.168.3.10:8787")
                self.assertFalse(data["feishu"]["configured"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

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


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from im_agent.models import OutboundMessage
from im_agent.platforms.feishu import (
    FeishuAdapter,
    FeishuSettings,
    build_feishu_text_payload,
    parse_feishu_message_content,
)


class FakeFeishuHandler(BaseHTTPRequestHandler):
    auth_calls = 0
    bot_info_calls = 0
    image_upload_calls = 0
    send_calls = 0
    last_path = ""
    last_auth_body = {}
    last_send_body = {}
    send_bodies = []
    last_image_upload_content_type = ""
    last_image_upload_body = b""

    def do_GET(self) -> None:  # noqa: N802
        type(self).last_path = self.path
        if self.path == "/open-apis/bot/v3/info":
            type(self).bot_info_calls += 1
            response = {
                "code": 0,
                "msg": "success",
                "data": {
                    "app_name": "Harbor Bot",
                    "tenant_key": "tenant_key_123",
                    "open_id": "ou_bot_123",
                    "user_id": "bot_user_123",
                },
            }
            encoded = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        body = raw_body.decode("utf-8", errors="replace")
        payload = json.loads(body) if body and not self.path.endswith("/open-apis/im/v1/images") else {}
        type(self).last_path = self.path
        if self.path.endswith("/open-apis/auth/v3/tenant_access_token/internal"):
            type(self).auth_calls += 1
            type(self).last_auth_body = payload
            response = {
                "code": 0,
                "msg": "success",
                "tenant_access_token": "tenant_token_123",
                "expire": 7200,
            }
        elif self.path.endswith("/open-apis/im/v1/images"):
            type(self).image_upload_calls += 1
            type(self).last_image_upload_content_type = self.headers.get("Content-Type", "")
            type(self).last_image_upload_body = raw_body
            response = {
                "code": 0,
                "msg": "success",
                "data": {"image_key": "img_uploaded_123"},
            }
        elif self.path.startswith("/open-apis/im/v1/messages"):
            type(self).send_calls += 1
            type(self).last_send_body = payload
            type(self).send_bodies.append(payload)
            response = {
                "code": 0,
                "msg": "success",
                "data": {"message_id": "om_sent_123"},
            }
        else:
            self.send_response(404)
            self.end_headers()
            return

        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class FakeWebsocketRuntime:
    started = 0
    stopped = 0
    payload: dict | None = None

    def __init__(self, *, settings, on_event, on_connected, on_disconnected) -> None:
        self.settings = settings
        self.on_event = on_event
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self._stop = threading.Event()

    def start(self) -> None:
        type(self).started += 1
        self.on_connected()
        if type(self).payload is not None:
            self.on_event(type(self).payload)
        while not self._stop.wait(0.01):
            continue
        self.on_disconnected()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds
        type(self).stopped += 1
        self._stop.set()


class FeishuHelperTests(unittest.TestCase):
    def test_parse_feishu_message_content_accepts_json_string(self) -> None:
        payload = parse_feishu_message_content('{"text":"hello"}')
        self.assertEqual(payload["text"], "hello")

    def test_build_feishu_text_payload(self) -> None:
        payload = build_feishu_text_payload("oc_123", "hello")
        self.assertEqual(payload["receive_id"], "oc_123")
        self.assertEqual(payload["msg_type"], "text")
        self.assertEqual(json.loads(payload["content"])["text"], "hello")


class FeishuAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeWebsocketRuntime.started = 0
        FakeWebsocketRuntime.stopped = 0
        FakeWebsocketRuntime.payload = None
        FakeFeishuHandler.auth_calls = 0
        FakeFeishuHandler.bot_info_calls = 0
        FakeFeishuHandler.image_upload_calls = 0
        FakeFeishuHandler.send_calls = 0
        FakeFeishuHandler.last_path = ""
        FakeFeishuHandler.last_auth_body = {}
        FakeFeishuHandler.last_send_body = {}
        FakeFeishuHandler.send_bodies = []
        FakeFeishuHandler.last_image_upload_content_type = ""
        FakeFeishuHandler.last_image_upload_body = b""

    def test_normalize_direct_message_from_raw_event(self) -> None:
        adapter = FeishuAdapter(
            FeishuSettings(
                app_id="cli_xxx",
                app_secret="secret_xxx",
                bot_open_id="ou_bot_1",
            )
        )
        message = adapter.normalize_inbound(
            {
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_user_1"}},
                    "message": {
                        "chat_id": "oc_chat_1",
                        "chat_type": "p2p",
                        "message_type": "text",
                        "content": '{"text":"你好"}',
                    },
                },
            }
        )

        self.assertEqual(message.platform, "feishu")
        self.assertEqual(message.chat_id, "oc_chat_1")
        self.assertEqual(message.user_id, "ou_user_1")
        self.assertEqual(message.text, "你好")

    def test_group_message_requires_explicit_mention(self) -> None:
        adapter = FeishuAdapter(
            FeishuSettings(
                app_id="cli_xxx",
                app_secret="secret_xxx",
                bot_open_id="ou_bot_1",
                group_policy="open",
            )
        )

        with self.assertRaises(ValueError):
            adapter.normalize_inbound(
                {
                    "header": {"event_type": "im.message.receive_v1"},
                    "event": {
                        "sender": {"sender_id": {"open_id": "ou_user_1"}},
                        "message": {
                            "chat_id": "oc_group_1",
                            "chat_type": "group",
                            "message_type": "text",
                            "content": '{"text":"hello group"}',
                            "mentions": [],
                        },
                    },
                }
            )

    def test_group_message_with_bot_mention_passes(self) -> None:
        adapter = FeishuAdapter(
            FeishuSettings(
                app_id="cli_xxx",
                app_secret="secret_xxx",
                bot_open_id="ou_bot_1",
                group_policy="open",
            )
        )
        message = adapter.normalize_inbound(
            {
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_user_1"}},
                    "message": {
                        "chat_id": "oc_group_1",
                        "chat_type": "group",
                        "message_type": "text",
                        "content": '{"text":"@bot 你好"}',
                        "mentions": [{"id": {"open_id": "ou_bot_1"}, "name": "Bot"}],
                    },
                },
            }
        )

        self.assertEqual(message.chat_id, "oc_group_1")
        self.assertEqual(message.text, "@bot 你好")

    def test_outbound_delivery_payload_is_protocol_facing(self) -> None:
        adapter = FeishuAdapter(
            FeishuSettings(
                app_id="cli_xxx",
                app_secret="secret_xxx",
                connection_mode="websocket",
                enable_live_send=False,
            )
        )
        payload = adapter.send_outbound(
            OutboundMessage(platform="feishu", chat_id="oc_chat_1", text="reply")
        )

        self.assertEqual(payload["delivery"], "feishu")
        self.assertFalse(payload["sent"])
        self.assertEqual(payload["request"]["receive_id"], "oc_chat_1")

    def test_url_verification_response_echoes_challenge(self) -> None:
        adapter = FeishuAdapter(
            FeishuSettings(
                app_id="cli_xxx",
                app_secret="secret_xxx",
                verification_token="verify_123",
            )
        )
        response = adapter.build_url_verification_response(
            {
                "type": "url_verification",
                "token": "verify_123",
                "challenge": "challenge_abc",
            }
        )

        self.assertEqual(response["challenge"], "challenge_abc")

    def test_websocket_connect_starts_runtime_and_forwards_events(self) -> None:
        received_payloads: list[dict] = []
        FakeWebsocketRuntime.payload = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user_1"}},
                "message": {
                    "chat_id": "oc_chat_1",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"hello from ws"}',
                },
            },
        }
        adapter = FeishuAdapter(
            FeishuSettings(
                app_id="cli_xxx",
                app_secret="secret_xxx",
                connection_mode="websocket",
            ),
            websocket_runtime_factory=FakeWebsocketRuntime,
        )

        adapter.connect(received_payloads.append)
        time.sleep(0.05)

        self.assertEqual(FakeWebsocketRuntime.started, 1)
        self.assertEqual(len(received_payloads), 1)
        self.assertTrue(adapter.transport_status()["connected"])

        adapter.disconnect()
        time.sleep(0.02)

        self.assertGreaterEqual(FakeWebsocketRuntime.stopped, 1)
        self.assertFalse(adapter.transport_status()["connected"])

    def test_live_send_posts_to_feishu_open_platform(self) -> None:
        FakeFeishuHandler.auth_calls = 0
        FakeFeishuHandler.bot_info_calls = 0
        FakeFeishuHandler.send_calls = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFeishuHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            adapter = FeishuAdapter(
                FeishuSettings(
                    app_id="cli_xxx",
                    app_secret="secret_xxx",
                    connection_mode="webhook",
                    base_url=base_url,
                    auth_base_url=base_url,
                    enable_live_send=True,
                )
            )
            payload = adapter.send_outbound(
                OutboundMessage(platform="feishu", chat_id="oc_chat_1", text="reply")
            )

            self.assertTrue(payload["sent"])
            self.assertEqual(payload["message_id"], "om_sent_123")
            self.assertEqual(FakeFeishuHandler.auth_calls, 1)
            self.assertEqual(FakeFeishuHandler.send_calls, 1)
            self.assertEqual(FakeFeishuHandler.last_auth_body["app_id"], "cli_xxx")
            self.assertEqual(FakeFeishuHandler.last_send_body["receive_id"], "oc_chat_1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_live_send_uploads_native_image_and_sends_image_message(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFeishuHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                image_path = Path(tmp) / "scene-001.jpg"
                image_path.write_bytes(b"fake-jpeg")
                base_url = f"http://127.0.0.1:{server.server_port}"
                adapter = FeishuAdapter(
                    FeishuSettings(
                        app_id="cli_xxx",
                        app_secret="secret_xxx",
                        connection_mode="webhook",
                        base_url=base_url,
                        auth_base_url=base_url,
                        enable_live_send=True,
                    )
                )

                payload = adapter.send_outbound(
                    OutboundMessage(
                        platform="feishu",
                        chat_id="oc_chat_1",
                        text="已找到与“春天”相关的 1 张图片。",
                        attachments=[
                            {
                                "kind": "image",
                                "label": "scene-001.jpg",
                                "mime_type": "image/jpeg",
                                "path": str(image_path),
                            }
                        ],
                        metadata={"source": "harborbeacon"},
                    )
                )

            self.assertTrue(payload["sent"])
            self.assertEqual(payload["message_id"], "om_sent_123")
            self.assertEqual(payload["metadata"]["native_attachment_kind"], "image")
            self.assertTrue(payload["metadata"]["native_image_reply"])
            self.assertEqual(payload["metadata"]["native_attachment_count"], 1)
            self.assertEqual(FakeFeishuHandler.auth_calls, 1)
            self.assertEqual(FakeFeishuHandler.image_upload_calls, 1)
            self.assertEqual(FakeFeishuHandler.send_calls, 2)
            self.assertIn("multipart/form-data", FakeFeishuHandler.last_image_upload_content_type)
            self.assertIn(b'name="image_type"', FakeFeishuHandler.last_image_upload_body)
            self.assertIn(b"message", FakeFeishuHandler.last_image_upload_body)
            self.assertEqual(FakeFeishuHandler.send_bodies[0]["msg_type"], "text")
            self.assertEqual(FakeFeishuHandler.send_bodies[1]["msg_type"], "image")
            image_content = json.loads(FakeFeishuHandler.send_bodies[1]["content"])
            self.assertEqual(image_content["image_key"], "img_uploaded_123")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_fetch_bot_info_uses_validated_token(self) -> None:
        FakeFeishuHandler.auth_calls = 0
        FakeFeishuHandler.bot_info_calls = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFeishuHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            adapter = FeishuAdapter(
                FeishuSettings(
                    app_id="cli_xxx",
                    app_secret="secret_xxx",
                    base_url=base_url,
                    auth_base_url=base_url,
                )
            )
            bot_info = adapter.fetch_bot_info()

            self.assertEqual(bot_info["app_name"], "Harbor Bot")
            self.assertEqual(bot_info["open_id"], "ou_bot_123")
            self.assertEqual(FakeFeishuHandler.auth_calls, 1)
            self.assertEqual(FakeFeishuHandler.bot_info_calls, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from im_agent.harborbeacon import (
    HarborBeaconAdminClient,
    HarborBeaconTaskClient,
    build_harborbeacon_admin_client_from_env,
    build_harborbeacon_client_from_env,
    build_task_request,
)
from im_agent.models import InboundMessage


class CaptureHandler(BaseHTTPRequestHandler):
    request_path = ""
    request_headers = {}
    request_payload = {}
    response_payload = {
        "task_id": "task_server_123",
        "trace_id": "trace_server_123",
        "status": "needs_input",
        "prompt": "Please confirm the target room.",
        "result": {
            "message": "Please confirm the target room.",
            "data": {},
            "artifacts": [],
            "events": [],
            "next_actions": ["living room", "front door"],
        },
        "resume_token": "resume_server_123",
        "error": None,
    }

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        type(self).request_path = self.path
        type(self).request_headers = dict(self.headers.items())
        type(self).request_payload = json.loads(body) if body else {}

        encoded = json.dumps(type(self).response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class HarborBeaconContractTests(unittest.TestCase):
    def test_build_task_request_uses_v15_shape(self) -> None:
        inbound = InboundMessage(
            platform="feishu",
            chat_id="oc_demo_chat",
            user_id="ou_demo_user",
            text="scan cameras",
            message_id="om_123",
            chat_type="p2p",
            mentions=[{"id": "ou_bot_xxx", "name": "HarborBeacon Bot"}],
            attachments=[{"attachment_id": "att_001", "type": "image"}],
            raw_payload={
                "intent": {"domain": "camera", "action": "scan"},
                "args": {"zone": "front-door"},
                "entity_refs": {"camera_id": "cam-1"},
            },
        )

        payload = build_task_request(inbound, resume_token="resume_001")

        self.assertEqual(payload["source"]["channel"], "feishu")
        self.assertEqual(payload["source"]["surface"], "harborgate")
        self.assertTrue(payload["source"]["route_key"].startswith("gw_route_"))
        self.assertTrue(payload["source"]["session_id"].startswith("gw_sess_"))
        self.assertTrue(payload["step_id"].startswith("step_"))
        self.assertNotEqual(payload["step_id"], "step_01")
        self.assertEqual(payload["intent"]["domain"], "camera")
        self.assertEqual(payload["intent"]["action"], "scan")
        self.assertEqual(payload["intent"]["raw_text"], "scan cameras")
        self.assertEqual(payload["message"]["message_id"], "om_123")
        self.assertEqual(payload["message"]["chat_type"], "p2p")
        self.assertEqual(payload["message"]["mentions"][0]["id"], "ou_bot_xxx")
        self.assertEqual(payload["args"]["zone"], "front-door")
        self.assertEqual(payload["args"]["resume_token"], "resume_001")
        self.assertEqual(payload["entity_refs"]["camera_id"], "cam-1")

    def test_build_task_request_uses_distinct_step_ids_per_message(self) -> None:
        first = InboundMessage(
            platform="feishu",
            chat_id="oc_demo_chat",
            user_id="ou_demo_user",
            text="scan cameras",
            message_id="om_123",
        )
        second = InboundMessage(
            platform="feishu",
            chat_id="oc_demo_chat",
            user_id="ou_demo_user",
            text="scan cameras again",
            message_id="om_124",
        )

        first_payload = build_task_request(first)
        first_replay_payload = build_task_request(first)
        second_payload = build_task_request(second)

        self.assertEqual(first_payload["step_id"], first_replay_payload["step_id"])
        self.assertNotEqual(first_payload["step_id"], second_payload["step_id"])

    def test_build_task_request_preserves_harboros_service_restart_shape(self) -> None:
        inbound = InboundMessage(
            platform="feishu",
            chat_id="oc_harboros_chat",
            user_id="ou_harboros_user",
            text="restart ssh",
            message_id="om_harboros_restart",
            chat_type="group",
            raw_payload={
                "intent": {"domain": "service", "action": "restart"},
                "args": {"service_name": "ssh"},
                "entity_refs": {"resource": {"service_name": "ssh"}},
            },
        )

        payload = build_task_request(inbound, resume_token="resume_harboros_restart")

        self.assertEqual(payload["intent"]["domain"], "service")
        self.assertEqual(payload["intent"]["action"], "restart")
        self.assertEqual(payload["args"]["service_name"], "ssh")
        self.assertEqual(payload["args"]["resume_token"], "resume_harboros_restart")
        self.assertEqual(payload["entity_refs"]["resource"]["service_name"], "ssh")

    def test_build_task_request_uses_same_v15_shape_for_weixin_private_dm(self) -> None:
        inbound = InboundMessage(
            platform="weixin",
            chat_id="wx-user-1",
            user_id="wx-user-1",
            text="status ssh",
            message_id="wx-msg-1",
            chat_type="p2p",
            raw_payload={
                "context_token": "ctx-opaque-weixin",
                "intent": {"domain": "service", "action": "status"},
                "args": {"service_name": "ssh"},
            },
        )

        payload = build_task_request(inbound)

        self.assertEqual(payload["source"]["channel"], "weixin")
        self.assertEqual(payload["source"]["surface"], "harborgate")
        self.assertTrue(payload["source"]["route_key"].startswith("gw_route_"))
        self.assertTrue(payload["source"]["session_id"].startswith("gw_sess_"))
        self.assertEqual(payload["intent"]["domain"], "service")
        self.assertEqual(payload["intent"]["action"], "status")
        self.assertEqual(payload["args"]["service_name"], "ssh")
        self.assertEqual(payload["message"]["message_id"], "wx-msg-1")
        self.assertEqual(payload["message"]["chat_type"], "p2p")
        self.assertNotIn("context_token", payload["source"])
        self.assertNotIn("context_token", payload["args"])
        self.assertNotIn("context_token", payload["message"])

    def test_build_task_request_preserves_opaque_attachment_metadata(self) -> None:
        inbound = InboundMessage(
            platform="webhook",
            chat_id="room-opaque",
            user_id="alice",
            text="find the document",
            message_id="msg-opaque",
            attachments=[
                {
                    "type": "file",
                    "file_key": "file_opaque_123",
                    "name": "design-spec.pdf",
                    "mime_type": "application/pdf",
                    "download_url": "https://files.example/private?token=secret",
                },
                {
                    "type": "image",
                    "file_key": "image_opaque_456",
                    "name": "diagram.png",
                    "mime_type": "image/png",
                },
            ],
            raw_payload={
                "intent": {"domain": "knowledge", "action": "search"},
                "args": {"query": "floor plan"},
            },
        )

        payload = build_task_request(inbound)

        self.assertEqual(payload["message"]["message_id"], "msg-opaque")
        self.assertEqual(len(payload["message"]["attachments"]), 2)
        self.assertEqual(payload["message"]["attachments"][0]["file_key"], "file_opaque_123")
        self.assertEqual(payload["message"]["attachments"][0]["download_url"], "https://files.example/private?token=secret")
        self.assertEqual(payload["message"]["attachments"][1]["mime_type"], "image/png")
        self.assertEqual(payload["intent"]["domain"], "knowledge")
        self.assertEqual(payload["intent"]["action"], "search")

    def test_task_client_posts_contract_request_and_maps_response(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = HarborBeaconTaskClient(
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_token="secret-token",
            )
            inbound = InboundMessage(
                platform="webhook",
                chat_id="room-1",
                user_id="alice",
                text="connect the front camera",
                message_id="msg-1",
                raw_payload={"intent": {"domain": "camera", "action": "connect"}},
            )

            result = client.submit_turn(inbound, session_metadata={"resume_token": "resume_previous"})

            self.assertEqual(CaptureHandler.request_path, "/api/tasks")
            self.assertEqual(CaptureHandler.request_headers["X-Contract-Version"], "1.5")
            self.assertEqual(CaptureHandler.request_headers["Authorization"], "Bearer secret-token")
            self.assertEqual(CaptureHandler.request_payload["args"]["resume_token"], "resume_previous")
            self.assertEqual(CaptureHandler.request_payload["intent"]["domain"], "camera")
            self.assertEqual(result.status, "needs_input")
            self.assertEqual(result.resume_token, "resume_server_123")
            self.assertEqual(result.text, "Please confirm the target room.")
            self.assertEqual(result.next_actions, ["living room", "front door"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_task_client_maps_harboros_restart_needs_input_without_schema_changes(self) -> None:
        previous_response = dict(CaptureHandler.response_payload)
        CaptureHandler.response_payload = {
            "task_id": "task_harbor_restart",
            "trace_id": "trace_harbor_restart",
            "status": "needs_input",
            "prompt": "restart requires approval",
            "result": {
                "message": "restart requires approval",
                "data": {
                    "approval_ticket": {
                        "approval_id": "approval_harbor_restart_1",
                        "policy_ref": "service.restart",
                    }
                },
                "artifacts": [],
                "events": [],
                "next_actions": ["approval_token approval_harbor_restart_1"],
            },
            "resume_token": "resume_harbor_restart",
            "error": None,
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = HarborBeaconTaskClient(
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_token="secret-token",
            )
            inbound = InboundMessage(
                platform="webhook",
                chat_id="room-harbor-restart",
                user_id="alice",
                text="restart ssh",
                message_id="msg-harbor-restart",
                raw_payload={
                    "intent": {"domain": "service", "action": "restart"},
                    "args": {"service_name": "ssh"},
                },
            )

            result = client.submit_turn(
                inbound,
                session_metadata={"resume_token": "resume_prior_turn"},
            )

            self.assertEqual(CaptureHandler.request_payload["intent"]["domain"], "service")
            self.assertEqual(CaptureHandler.request_payload["intent"]["action"], "restart")
            self.assertEqual(CaptureHandler.request_payload["args"]["service_name"], "ssh")
            self.assertEqual(
                CaptureHandler.request_payload["args"]["resume_token"],
                "resume_prior_turn",
            )
            self.assertEqual(result.status, "needs_input")
            self.assertEqual(result.resume_token, "resume_harbor_restart")
            self.assertEqual(result.text, "restart requires approval")
            self.assertEqual(
                result.next_actions,
                ["approval_token approval_harbor_restart_1"],
            )
        finally:
            CaptureHandler.response_payload = previous_response
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_env_builder_reads_harborbeacon_settings_only(self) -> None:
        previous = {
            "HARBORBEACON_TASK_API_URL": os.environ.get("HARBORBEACON_TASK_API_URL"),
            "HARBORBEACON_SOURCE_SURFACE": os.environ.get("HARBORBEACON_SOURCE_SURFACE"),
        }
        os.environ["HARBORBEACON_TASK_API_URL"] = "http://127.0.0.1:4175"
        os.environ["HARBORBEACON_SOURCE_SURFACE"] = "im_gateway"
        self.addCleanup(self._restore_env, previous)

        client = build_harborbeacon_client_from_env()
        self.assertIsNotNone(client)
        assert client is not None
        self.assertEqual(client.base_url, "http://127.0.0.1:4175")
        self.assertEqual(client.source_surface, "im_gateway")

    def test_admin_client_posts_notification_target_with_service_auth(self) -> None:
        previous_response = dict(CaptureHandler.response_payload)
        CaptureHandler.response_payload = {
            "targets": [
                {
                    "target_id": "target_001",
                    "label": "Weixin DM abc123",
                    "route_key": "gw_route_abc123",
                    "platform_hint": "weixin",
                    "is_default": True,
                }
            ]
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = HarborBeaconAdminClient(
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_token="service-token",
            )

            result = client.upsert_notification_target(
                label="Weixin DM abc123",
                route_key="gw_route_abc123",
                platform_hint="weixin",
            )

            self.assertEqual(CaptureHandler.request_path, "/api/admin/notification-targets")
            self.assertEqual(CaptureHandler.request_headers["X-Contract-Version"], "1.5")
            self.assertEqual(CaptureHandler.request_headers["Authorization"], "Bearer service-token")
            self.assertEqual(CaptureHandler.request_payload["label"], "Weixin DM abc123")
            self.assertEqual(CaptureHandler.request_payload["route_key"], "gw_route_abc123")
            self.assertEqual(CaptureHandler.request_payload["platform_hint"], "weixin")
            self.assertEqual(result["targets"][0]["route_key"], "gw_route_abc123")
        finally:
            CaptureHandler.response_payload = previous_response
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_admin_env_builder_falls_back_to_task_api_url_and_token(self) -> None:
        previous = {
            "HARBORBEACON_TASK_API_URL": os.environ.get("HARBORBEACON_TASK_API_URL"),
            "HARBORBEACON_TASK_API_TOKEN": os.environ.get("HARBORBEACON_TASK_API_TOKEN"),
            "HARBORBEACON_ADMIN_API_URL": os.environ.get("HARBORBEACON_ADMIN_API_URL"),
            "HARBORBEACON_ADMIN_API_TOKEN": os.environ.get("HARBORBEACON_ADMIN_API_TOKEN"),
            "IM_AGENT_SERVICE_TOKEN": os.environ.get("IM_AGENT_SERVICE_TOKEN"),
        }
        os.environ["HARBORBEACON_TASK_API_URL"] = "http://127.0.0.1:4175/api/tasks"
        os.environ["HARBORBEACON_TASK_API_TOKEN"] = "task-token"
        os.environ.pop("HARBORBEACON_ADMIN_API_URL", None)
        os.environ.pop("HARBORBEACON_ADMIN_API_TOKEN", None)
        os.environ.pop("IM_AGENT_SERVICE_TOKEN", None)
        self.addCleanup(self._restore_env, previous)

        client = build_harborbeacon_admin_client_from_env()
        self.assertIsNotNone(client)
        assert client is not None
        self.assertEqual(client.base_url, "http://127.0.0.1:4175")
        self.assertEqual(client.api_token, "task-token")

    def test_admin_env_builder_prefers_explicit_admin_url_and_token(self) -> None:
        previous = {
            "HARBORBEACON_TASK_API_URL": os.environ.get("HARBORBEACON_TASK_API_URL"),
            "HARBORBEACON_TASK_API_TOKEN": os.environ.get("HARBORBEACON_TASK_API_TOKEN"),
            "HARBORBEACON_ADMIN_API_URL": os.environ.get("HARBORBEACON_ADMIN_API_URL"),
            "HARBORBEACON_ADMIN_API_TOKEN": os.environ.get("HARBORBEACON_ADMIN_API_TOKEN"),
        }
        os.environ["HARBORBEACON_TASK_API_URL"] = "http://127.0.0.1:4175/api/tasks"
        os.environ["HARBORBEACON_TASK_API_TOKEN"] = "task-token"
        os.environ["HARBORBEACON_ADMIN_API_URL"] = "http://127.0.0.1:4174"
        os.environ["HARBORBEACON_ADMIN_API_TOKEN"] = "admin-token"
        self.addCleanup(self._restore_env, previous)

        client = build_harborbeacon_admin_client_from_env()
        self.assertIsNotNone(client)
        assert client is not None
        self.assertEqual(client.base_url, "http://127.0.0.1:4174")
        self.assertEqual(client.api_token, "admin-token")

    @staticmethod
    def _restore_env(previous: dict[str, str | None]) -> None:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()

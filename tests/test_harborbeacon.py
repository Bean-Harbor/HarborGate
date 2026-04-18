import json
import os
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from im_agent.harborbeacon import (
    HarborBeaconTaskClient,
    build_harborbeacon_client_from_env,
    build_harbornas_client_from_env,
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

    def test_legacy_env_alias_builds_client_with_warning(self) -> None:
        self.addCleanup(warnings.resetwarnings)
        warnings.simplefilter("always")
        previous = {
            "HARBORBEACON_TASK_API_URL": None,
            "HARBORNAS_TASK_API_URL": None,
            "HARBORBEACON_SOURCE_SURFACE": None,
            "HARBORNAS_SOURCE_SURFACE": None,
        }
        for key in previous:
            previous[key] = os.environ.get(key)
            os.environ.pop(key, None)
        self.addCleanup(self._restore_env, previous)

        os.environ["HARBORNAS_TASK_API_URL"] = "http://127.0.0.1:4175"
        os.environ["HARBORNAS_SOURCE_SURFACE"] = "im_gateway"

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client = build_harborbeacon_client_from_env()

        self.assertIsNotNone(client)
        assert client is not None
        self.assertEqual(client.base_url, "http://127.0.0.1:4175")
        self.assertEqual(client.source_surface, "im_gateway")
        self.assertTrue(any("deprecated" in str(item.message).lower() for item in caught))

    def test_deprecated_builder_alias_warns_and_delegates(self) -> None:
        self.addCleanup(warnings.resetwarnings)
        warnings.simplefilter("always")
        previous = {
            "HARBORBEACON_TASK_API_URL": os.environ.get("HARBORBEACON_TASK_API_URL"),
        }
        os.environ["HARBORBEACON_TASK_API_URL"] = "http://127.0.0.1:4175"
        self.addCleanup(self._restore_env, previous)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client = build_harbornas_client_from_env()

        self.assertIsNotNone(client)
        self.assertTrue(any("deprecated" in str(item.message).lower() for item in caught))

    @staticmethod
    def _restore_env(previous: dict[str, str | None]) -> None:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()

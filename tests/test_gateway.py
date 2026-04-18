import tempfile
import unittest

from im_agent.brain import RuleBasedBrain
from im_agent.errors import GatewayContractError
from im_agent.gateway import GatewayService
from im_agent.harbornas import TaskTurnResult
from im_agent.platforms.webhook import WebhookAdapter
from im_agent.session_store import FileSessionStore


class FakeTaskClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._call_count = 0

    def submit_turn(self, incoming, *, session_metadata=None):  # type: ignore[no-untyped-def]
        metadata = dict(session_metadata or {})
        self.calls.append({"incoming": incoming, "session_metadata": metadata})
        self._call_count += 1
        if self._call_count == 1:
            return TaskTurnResult(
                text="Which room should I scan?",
                task_id="task_first",
                trace_id="trace_first",
                status="needs_input",
                route_key="gw_route_room1",
                resume_token="resume_first",
            )
        return TaskTurnResult(
            text="Front door scan started.",
            task_id="task_second",
            trace_id="trace_second",
            status="completed",
            route_key="gw_route_room1",
        )


class GatewayServiceTests(unittest.TestCase):
    @staticmethod
    def _notification_payload(route_key: str, *, idempotency_key: str = "idem-1", body: str = "Done") -> dict:
        return {
            "notification_id": "notif-1",
            "trace_id": "trace-1",
            "destination": {
                "kind": "conversation",
                "route_key": route_key,
            },
            "content": {
                "title": "Front Door",
                "body": body,
                "payload_format": "plain_text",
                "structured_payload": {},
                "attachments": [],
            },
            "delivery": {
                "mode": "send",
                "reply_to_message_id": "",
                "update_message_id": "",
                "idempotency_key": idempotency_key,
            },
        }

    def test_round_trip_creates_reply_and_persists_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            gateway.register_adapter(WebhookAdapter())

            first = gateway.handle_inbound(
                "webhook",
                {
                    "platform": "feishu",
                    "chat_id": "room-1",
                    "user_id": "alice",
                    "text": "hello there",
                },
            )
            second = gateway.handle_inbound(
                "webhook",
                {
                    "platform": "feishu",
                    "chat_id": "room-1",
                    "user_id": "alice",
                    "text": "can you still see history",
                },
            )

            self.assertEqual(first["chat_id"], "room-1")
            self.assertIn("hello there", first["text"])
            self.assertIn("stored turns", second["text"])

    def test_missing_adapter_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )

            with self.assertRaises(ValueError):
                gateway.handle_inbound("missing", {"chat_id": "x", "text": "y"})

    def test_task_client_path_persists_and_reuses_resume_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_client = FakeTaskClient()
            store = FileSessionStore(tmp, max_turns=10)
            gateway = GatewayService(
                store=store,
                brain=RuleBasedBrain(),
                task_client=task_client,
            )
            gateway.register_adapter(WebhookAdapter())

            first = gateway.handle_inbound(
                "webhook",
                {
                    "platform": "feishu",
                    "chat_id": "room-1",
                    "user_id": "alice",
                    "text": "scan camera",
                    "message_id": "msg-1",
                },
            )
            second = gateway.handle_inbound(
                "webhook",
                {
                    "platform": "feishu",
                    "chat_id": "room-1",
                    "user_id": "alice",
                    "text": "front door",
                    "message_id": "msg-2",
                },
            )

            self.assertEqual(first["text"], "Which room should I scan?")
            self.assertEqual(second["text"], "Front door scan started.")
            self.assertEqual(task_client.calls[0]["session_metadata"], {})
            self.assertEqual(task_client.calls[1]["session_metadata"]["resume_token"], "resume_first")
            self.assertEqual(first["metadata"]["source"], "harbornas")
            self.assertEqual(first["metadata"]["resume_token"], "resume_first")
            self.assertEqual(second["metadata"]["task_id"], "task_second")
            self.assertNotIn("resume_token", store.load_metadata("feishu", "room-1"))

    def test_notification_delivery_uses_registered_route_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(tmp, max_turns=10)
            gateway = GatewayService(store=store, brain=RuleBasedBrain())
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

            route_key = str(store.load_metadata("feishu", "room-1")["route_key"])
            payload = self._notification_payload(route_key)

            first = gateway.handle_notification_delivery(payload)
            second = gateway.handle_notification_delivery(payload)

            self.assertTrue(first["ok"])
            self.assertEqual(first["status"], "sent")
            self.assertEqual(first["platform"], "feishu")
            self.assertEqual(first, second)

    def test_notification_conflicting_idempotency_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(tmp, max_turns=10)
            gateway = GatewayService(store=store, brain=RuleBasedBrain())
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

            route_key = str(store.load_metadata("feishu", "room-1")["route_key"])
            gateway.handle_notification_delivery(self._notification_payload(route_key, idempotency_key="idem-conflict", body="first"))

            with self.assertRaises(GatewayContractError) as ctx:
                gateway.handle_notification_delivery(
                    self._notification_payload(route_key, idempotency_key="idem-conflict", body="second")
                )

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_notification_unknown_route_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            gateway.register_adapter(WebhookAdapter())

            with self.assertRaises(GatewayContractError) as ctx:
                gateway.handle_notification_delivery(self._notification_payload("gw_route_missing"))

            self.assertEqual(ctx.exception.status_code, 404)
            self.assertEqual(ctx.exception.code, "ROUTE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()

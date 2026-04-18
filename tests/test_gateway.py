import tempfile
import unittest

from im_agent.brain import RuleBasedBrain
from im_agent.gateway import GatewayService
from im_agent.platforms.webhook import WebhookAdapter
from im_agent.session_store import FileSessionStore


class GatewayServiceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

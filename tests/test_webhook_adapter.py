import unittest

from im_agent.platforms.webhook import WebhookAdapter


class WebhookAdapterTests(unittest.TestCase):
    def test_normalize_inbound_uses_defaults(self) -> None:
        adapter = WebhookAdapter()
        message = adapter.normalize_inbound(
            {
                "chat_id": "demo",
                "text": "ping",
            }
        )

        self.assertEqual(message.platform, "webhook")
        self.assertEqual(message.user_id, "anonymous")
        self.assertEqual(message.text, "ping")

    def test_normalize_inbound_requires_chat_and_text(self) -> None:
        adapter = WebhookAdapter()

        with self.assertRaises(ValueError):
            adapter.normalize_inbound({"text": "hello"})

        with self.assertRaises(ValueError):
            adapter.normalize_inbound({"chat_id": "demo"})


if __name__ == "__main__":
    unittest.main()

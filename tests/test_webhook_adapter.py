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

    def test_normalize_inbound_preserves_attachment_metadata_opaque(self) -> None:
        adapter = WebhookAdapter()
        message = adapter.normalize_inbound(
            {
                "chat_id": "demo",
                "text": "find the file",
                "attachments": [
                    {
                        "type": "image",
                        "file_key": "file_opaque_123",
                        "name": "floor-plan.png",
                        "mime_type": "image/png",
                        "download_url": "https://files.example/private?token=secret",
                    }
                ],
                "metadata": {
                    "transport_hint": "opaque",
                },
            }
        )

        self.assertEqual(len(message.attachments), 1)
        self.assertEqual(message.attachments[0]["file_key"], "file_opaque_123")
        self.assertEqual(message.attachments[0]["download_url"], "https://files.example/private?token=secret")
        self.assertEqual(message.metadata["transport_hint"], "opaque")
        self.assertEqual(message.raw_payload["attachments"][0]["name"], "floor-plan.png")

    def test_normalize_inbound_requires_chat_and_text(self) -> None:
        adapter = WebhookAdapter()

        with self.assertRaises(ValueError):
            adapter.normalize_inbound({"text": "hello"})

        with self.assertRaises(ValueError):
            adapter.normalize_inbound({"chat_id": "demo"})


if __name__ == "__main__":
    unittest.main()

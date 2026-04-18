import tempfile
import unittest

from im_agent.models import OutboundMessage
from im_agent.platforms.weixin import (
    ContextTokenStore,
    WeixinAdapter,
    build_send_message_payload,
    extract_text_from_item_list,
    save_weixin_account,
    split_text_for_weixin,
)


class WeixinHelpersTests(unittest.TestCase):
    def test_extract_text_from_item_list(self) -> None:
        text = extract_text_from_item_list(
            [
                {"type": 1, "text_item": {"text": "hello"}},
                {"type": 99, "ignored": True},
                {"type": 1, "text_item": {"text": "world"}},
            ]
        )
        self.assertEqual(text, "hello\nworld")

    def test_build_send_message_payload_includes_context_token(self) -> None:
        payload = build_send_message_payload(
            to_user_id="wx-user-1",
            text="hi",
            context_token="ctx-123",
            client_id="client-1",
        )
        self.assertEqual(payload["msg"]["to_user_id"], "wx-user-1")
        self.assertEqual(payload["msg"]["client_id"], "client-1")
        self.assertEqual(payload["msg"]["context_token"], "ctx-123")

    def test_split_text_for_weixin_preserves_full_content(self) -> None:
        content = "A" * 950 + "\n" + "B" * 200
        chunks = split_text_for_weixin(content, max_length=500)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks).replace("\n", ""), content.replace("\n", ""))


class WeixinAdapterTests(unittest.TestCase):
    def test_normalize_inbound_stores_context_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
                user_id="self-1",
            )
            adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")
            message = adapter.normalize_inbound(
                {
                    "from_user_id": "wx-user-1",
                    "context_token": "ctx-001",
                    "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                }
            )

            self.assertEqual(message.chat_id, "wx-user-1")
            self.assertEqual(message.text, "你好")
            token_store = ContextTokenStore(tmp, "bot-1")
            self.assertEqual(token_store.get("wx-user-1"), "ctx-001")

    def test_normalize_inbound_rejects_groups_for_now(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
            )
            adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")
            with self.assertRaises(ValueError):
                adapter.normalize_inbound(
                    {
                        "from_user_id": "wx-user-1",
                        "room_id": "room-1",
                        "item_list": [{"type": 1, "text_item": {"text": "group msg"}}],
                    }
                )

    def test_send_outbound_requires_context_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
            )
            adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")
            with self.assertRaises(RuntimeError):
                adapter.send_outbound(
                    OutboundMessage(platform="weixin", chat_id="wx-user-1", text="reply")
                )


if __name__ == "__main__":
    unittest.main()

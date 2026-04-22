import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from im_agent.models import OutboundMessage
from im_agent.platforms.weixin import (
    ContextTokenStore,
    ProcessedMessageStore,
    WeixinAdapter,
    build_send_message_payload,
    extract_weixin_message_id,
    extract_text_from_item_list,
    load_sync_buf,
    load_weixin_transport_state,
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

    def test_extract_weixin_message_id_prefers_msg_id(self) -> None:
        message_id = extract_weixin_message_id({"msg_id": "msg-1", "client_id": "client-1"})
        self.assertEqual(message_id, "msg-1")

    def test_processed_message_store_persists_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProcessedMessageStore(tmp, "bot-1", max_items=2)
            store.add("msg-1")
            store.add("msg-2")
            store.add("msg-3")

            restored = ProcessedMessageStore(tmp, "bot-1", max_items=2)
            self.assertFalse(restored.contains("msg-1"))
            self.assertTrue(restored.contains("msg-2"))
            self.assertTrue(restored.contains("msg-3"))


class WeixinAdapterTests(unittest.TestCase):
    def test_adapter_restores_saved_account_and_poll_updates_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
                user_id="self-1",
            )
            adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")

            self.assertTrue(adapter.configured)
            self.assertEqual(adapter.base_url, "https://example.com")
            self.assertEqual(adapter.user_id, "self-1")

            with patch(
                "im_agent.platforms.weixin.post_json",
                return_value={
                    "get_updates_buf": "cursor-next",
                    "msgs": [
                        {
                            "msg_id": "wx-msg-1",
                            "from_user_id": "wx-user-1",
                            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
                        }
                    ],
                },
            ) as mocked_post:
                messages = adapter.poll_updates(timeout_ms=1000)

            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["msg_id"], "wx-msg-1")
            self.assertEqual(load_sync_buf(tmp, "bot-1"), "cursor-next")
            transport = adapter.transport_status()
            self.assertEqual(transport["status"], "polling_idle")
            self.assertTrue(transport["connected"])
            self.assertEqual(transport["last_getupdates_buf"], "cursor-next")
            self.assertEqual(transport["last_getupdates_count"], 1)
            self.assertEqual(transport["last_getupdates_message_ids"], ["wx-msg-1"])
            self.assertEqual(transport["last_getupdates_error"], "")
            mocked_post.assert_called_once()

    def test_poll_updates_treats_idle_read_timeout_as_healthy_empty_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
                user_id="self-1",
            )
            adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")

            with patch(
                "im_agent.platforms.weixin.post_json",
                side_effect=TimeoutError("The read operation timed out"),
            ):
                messages = adapter.poll_updates(timeout_ms=1000)

            self.assertEqual(messages, [])
            transport = adapter.transport_status()
            self.assertEqual(transport["status"], "polling_idle")
            self.assertTrue(transport["connected"])
            self.assertEqual(transport["last_poll_outcome"], "idle_timeout")
            self.assertEqual(transport["last_getupdates_error"], "")
            self.assertEqual(transport["last_getupdates_count"], 0)
            self.assertEqual(transport["last_private_text_message_count"], 0)

    def test_poll_updates_preserves_real_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
                user_id="self-1",
            )
            adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")

            with patch(
                "im_agent.platforms.weixin.post_json",
                side_effect=URLError("network down"),
            ):
                with self.assertRaises(URLError):
                    adapter.poll_updates(timeout_ms=1000)

            transport = adapter.transport_status()
            self.assertEqual(transport["status"], "error")
            self.assertFalse(transport["connected"])
            self.assertEqual(transport["last_poll_outcome"], "error")
            self.assertIn("network down", transport["last_getupdates_error"])

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
            transport = adapter.transport_status()
            self.assertEqual(transport["last_inbound_chat_id"], "wx-user-1")
            self.assertEqual(transport["last_inbound_message_id"], "")
            self.assertTrue(transport["last_inbound_at"])

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

    def test_send_outbound_records_successful_delivery_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
            )
            adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")
            assert adapter._context_tokens is not None
            adapter._context_tokens.set("wx-user-1", "ctx-123")
            with patch("im_agent.platforms.weixin.post_json", return_value={}) as mocked_post:
                response = adapter.send_outbound(
                    OutboundMessage(platform="weixin", chat_id="wx-user-1", text="reply")
                )

            transport = adapter.transport_status()
            self.assertTrue(response["sent"])
            self.assertEqual(response["provider_message_id"], response["message_id"])
            self.assertEqual(transport["last_send_status"], "sent")
            self.assertEqual(transport["last_send_context_token_used"], True)
            self.assertEqual(transport["last_send_error"], "")
            self.assertTrue(transport["last_send_provider_message_id"])
            self.assertEqual(mocked_post.call_count, 1)

    def test_duplicate_update_tracking_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
            )
            adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")
            payload = {
                "msg_id": "msg-123",
                "from_user_id": "wx-user-1",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            }

            self.assertFalse(adapter.is_duplicate_update(payload))
            adapter.mark_update_processed(payload)
            self.assertTrue(adapter.is_duplicate_update(payload))

            restored = WeixinAdapter(state_dir=tmp, account_id="bot-1")
            self.assertTrue(restored.is_duplicate_update(payload))

    def test_transport_state_is_persisted_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
            )
            adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")

            with patch(
                "im_agent.platforms.weixin.post_json",
                side_effect=TimeoutError("The read operation timed out"),
            ):
                adapter.poll_updates(timeout_ms=1000)

            persisted = load_weixin_transport_state(tmp, "bot-1")
            assert isinstance(persisted, dict)
            self.assertEqual(persisted["last_poll_outcome"], "idle_timeout")
            self.assertTrue(persisted["connected"])

            restored = WeixinAdapter(state_dir=tmp, account_id="bot-1")
            restored_transport = restored.transport_status()
            self.assertEqual(restored_transport["last_poll_outcome"], "idle_timeout")
            self.assertTrue(restored_transport["connected"])


if __name__ == "__main__":
    unittest.main()

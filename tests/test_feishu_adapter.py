import json
import unittest

from im_agent.models import OutboundMessage
from im_agent.platforms.feishu import (
    FeishuAdapter,
    FeishuSettings,
    build_feishu_text_payload,
    parse_feishu_message_content,
)


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
            )
        )
        payload = adapter.send_outbound(
            OutboundMessage(platform="feishu", chat_id="oc_chat_1", text="reply")
        )

        self.assertEqual(payload["delivery"], "feishu")
        self.assertFalse(payload["sent"])
        self.assertEqual(payload["request"]["receive_id"], "oc_chat_1")


if __name__ == "__main__":
    unittest.main()

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from im_agent.models import OutboundMessage
from im_agent.platforms.weixin import (
    ContextTokenStore,
    ProcessedMessageStore,
    WeixinAdapter,
    _upload_image_artifact_to_weixin,
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

    def test_upload_image_artifact_to_weixin_requests_thumbnail_and_uploads_both_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = tempfile.NamedTemporaryFile(dir=tmp, suffix=".jpg", delete=False)
            try:
                image_path.write(b"fake-jpeg-bytes")
                image_path.close()
                with patch(
                    "im_agent.platforms.weixin._resolve_ffmpeg_bin",
                    return_value="ffmpeg",
                ), patch(
                    "im_agent.platforms.weixin.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        args=["ffmpeg"],
                        returncode=0,
                        stdout=b"thumb-jpeg-bytes",
                        stderr=b"",
                    ),
                ), patch(
                    "im_agent.platforms.weixin.post_json",
                    return_value={
                        "upload_param": "orig-upload-param",
                        "thumb_upload_param": "thumb-upload-param",
                    },
                ) as mocked_post, patch(
                    "im_agent.platforms.weixin._upload_binary_to_cdn",
                    side_effect=["orig-download-param", "thumb-download-param"],
                ) as mocked_upload:
                    uploaded = _upload_image_artifact_to_weixin(
                        image_path=Path(image_path.name),
                        to_user_id="wx-user-1",
                        base_url="https://example.com",
                        token="secret",
                        cdn_base_url="https://cdn.example.com/c2c",
                    )

                request_payload = mocked_post.call_args.args[2]
                self.assertEqual(mocked_post.call_args.args[1], "ilink/bot/getuploadurl")
                self.assertEqual(request_payload["to_user_id"], "wx-user-1")
                self.assertEqual(request_payload["thumb_rawsize"], len(b"thumb-jpeg-bytes"))
                self.assertEqual(request_payload["no_need_thumb"], False)
                self.assertEqual(mocked_upload.call_count, 2)
                self.assertEqual(uploaded.original_download_param, "orig-download-param")
                self.assertEqual(uploaded.thumbnail_download_param, "thumb-download-param")
            finally:
                image_path.close()

    def test_send_outbound_native_image_requires_context_token(self) -> None:
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
                    OutboundMessage(
                        platform="weixin",
                        chat_id="wx-user-1",
                        text="已抓拍 Tapo 231 当前画面。",
                        attachments=[
                            {
                                "kind": "image",
                                "mime_type": "image/jpeg",
                                "path": "capture.jpg",
                            }
                        ],
                        metadata={"source": "harborbeacon"},
                    )
                )

    def test_send_outbound_native_image_records_successful_delivery_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
            )
            image_path = tempfile.NamedTemporaryFile(dir=tmp, suffix=".jpg", delete=False)
            try:
                image_path.write(b"fake-jpeg")
                image_path.close()
                adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")
                assert adapter._context_tokens is not None
                adapter._context_tokens.set("wx-user-1", "ctx-123")
                with patch(
                    "im_agent.platforms.weixin._upload_image_artifact_to_weixin",
                    return_value=type(
                        "UploadedImage",
                        (),
                        {
                            "original_download_param": "orig-download-param",
                            "thumbnail_download_param": "thumb-download-param",
                            "aeskey_hex": "0123456789abcdef0123456789abcdef",
                            "original_ciphertext_size": 112,
                            "thumbnail_ciphertext_size": 64,
                        },
                    )(),
                ), patch("im_agent.platforms.weixin.post_json", return_value={}) as mocked_post:
                    response = adapter.send_outbound(
                        OutboundMessage(
                            platform="weixin",
                            chat_id="wx-user-1",
                            text="已抓拍 Tapo 231 当前画面。",
                            attachments=[
                                {
                                    "kind": "image",
                                    "mime_type": "image/jpeg",
                                    "path": image_path.name,
                                }
                            ],
                            metadata={"source": "harborbeacon"},
                        )
                    )

                transport = adapter.transport_status()
                send_payload = mocked_post.call_args.args[2]
                self.assertTrue(response["sent"])
                self.assertEqual(transport["last_send_status"], "sent")
                self.assertEqual(transport["last_send_content_kind"], "text+image")
                self.assertEqual(transport["last_send_attachment_count"], 1)
                self.assertEqual(len(send_payload["msg"]["item_list"]), 2)
                self.assertEqual(send_payload["msg"]["item_list"][0]["type"], 1)
                self.assertEqual(send_payload["msg"]["item_list"][1]["type"], 2)
            finally:
                image_path.close()

    def test_send_outbound_native_image_fails_strictly_when_upload_step_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
            )
            image_path = tempfile.NamedTemporaryFile(dir=tmp, suffix=".jpg", delete=False)
            try:
                image_path.write(b"fake-jpeg")
                image_path.close()
                adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")
                assert adapter._context_tokens is not None
                adapter._context_tokens.set("wx-user-1", "ctx-123")
                with patch(
                    "im_agent.platforms.weixin._upload_image_artifact_to_weixin",
                    side_effect=RuntimeError("getuploadurl failed"),
                ):
                    with self.assertRaises(RuntimeError):
                        adapter.send_outbound(
                            OutboundMessage(
                                platform="weixin",
                                chat_id="wx-user-1",
                                text="已抓拍 Tapo 231 当前画面。",
                                attachments=[
                                    {
                                        "kind": "image",
                                        "mime_type": "image/jpeg",
                                        "path": image_path.name,
                                    }
                                ],
                                metadata={"source": "harborbeacon"},
                            )
                        )

                transport = adapter.transport_status()
                self.assertEqual(transport["last_send_status"], "failed")
                self.assertIn("getuploadurl failed", transport["last_send_error"])
            finally:
                image_path.close()

    def test_send_outbound_native_image_fails_strictly_when_sendmessage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_weixin_account(
                tmp,
                account_id="bot-1",
                token="secret",
                base_url="https://example.com",
            )
            image_path = tempfile.NamedTemporaryFile(dir=tmp, suffix=".jpg", delete=False)
            try:
                image_path.write(b"fake-jpeg")
                image_path.close()
                adapter = WeixinAdapter(state_dir=tmp, account_id="bot-1")
                assert adapter._context_tokens is not None
                adapter._context_tokens.set("wx-user-1", "ctx-123")
                with patch(
                    "im_agent.platforms.weixin._upload_image_artifact_to_weixin",
                    return_value=type(
                        "UploadedImage",
                        (),
                        {
                            "original_download_param": "orig-download-param",
                            "thumbnail_download_param": "thumb-download-param",
                            "aeskey_hex": "0123456789abcdef0123456789abcdef",
                            "original_ciphertext_size": 112,
                            "thumbnail_ciphertext_size": 64,
                        },
                    )(),
                ), patch(
                    "im_agent.platforms.weixin.post_json",
                    side_effect=RuntimeError("sendmessage failed"),
                ):
                    with self.assertRaises(RuntimeError):
                        adapter.send_outbound(
                            OutboundMessage(
                                platform="weixin",
                                chat_id="wx-user-1",
                                text="已抓拍 Tapo 231 当前画面。",
                                attachments=[
                                    {
                                        "kind": "image",
                                        "mime_type": "image/jpeg",
                                        "path": image_path.name,
                                    }
                                ],
                                metadata={"source": "harborbeacon"},
                            )
                        )

                transport = adapter.transport_status()
                self.assertEqual(transport["last_send_status"], "failed")
                self.assertIn("sendmessage failed", transport["last_send_error"])
            finally:
                image_path.close()

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

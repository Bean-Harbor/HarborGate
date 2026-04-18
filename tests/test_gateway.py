import tempfile
import unittest

from im_agent.brain import RuleBasedBrain
from im_agent.errors import GatewayContractError
from im_agent.gateway import GatewayService
from im_agent.harborbeacon import TaskTurnResult
from im_agent.models import InboundMessage, OutboundMessage
from im_agent.platforms.base import PlatformAdapter
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


class FakeReplayTaskClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit_turn(self, incoming, *, session_metadata=None):  # type: ignore[no-untyped-def]
        metadata = dict(session_metadata or {})
        self.calls.append({"incoming": incoming, "session_metadata": metadata})
        if incoming.message_id == "msg-1":
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


class FakeDeliveryAdapter(WebhookAdapter):
    def send_outbound(self, outbound):  # type: ignore[no-untyped-def]
        payload = dict(super().send_outbound(outbound))
        payload["message_id"] = "provider_msg_123"
        payload["provider_message_id"] = "provider_msg_123"
        return payload


class FakeRetrievalTaskClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit_turn(self, incoming, *, session_metadata=None):  # type: ignore[no-untyped-def]
        metadata = dict(session_metadata or {})
        self.calls.append({"incoming": incoming, "session_metadata": metadata})
        return TaskTurnResult(
            text="Here is the retrieval summary.",
            task_id="task_retrieval",
            trace_id="trace_retrieval",
            status="completed",
            route_key="gw_route_retrieval",
            response_payload={
                "task_id": "task_retrieval",
                "trace_id": "trace_retrieval",
                "status": "completed",
                "result": {
                    "message": "Here is the retrieval summary.",
                    "citations": [
                        {
                            "title": "Policy Index",
                            "snippet": "Use section 4",
                            "source": "kb-alpha",
                        },
                        {
                            "name": "Spec Sheet",
                            "summary": "Attachment located",
                        },
                        {
                            "id": "cit-3",
                            "headline": "Third citation",
                        },
                    ],
                    "artifacts": [
                        {
                            "filename": "diagram.png",
                            "mime_type": "image/png",
                            "id": "artifact-1",
                        },
                        {
                            "name": "report.pdf",
                            "kind": "file",
                            "label": "Quarterly report",
                        },
                    ],
                },
            },
        )


class FakePlainRetrievalTaskClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit_turn(self, incoming, *, session_metadata=None):  # type: ignore[no-untyped-def]
        metadata = dict(session_metadata or {})
        self.calls.append({"incoming": incoming, "session_metadata": metadata})
        return TaskTurnResult(
            text="HarborBeacon returned a plain reply.",
            task_id="task_plain_retrieval",
            trace_id="trace_plain_retrieval",
            status="completed",
            route_key="gw_route_plain_retrieval",
            response_payload={
                "task_id": "task_plain_retrieval",
                "trace_id": "trace_plain_retrieval",
                "status": "completed",
                "result": {
                    "message": "HarborBeacon returned a plain reply.",
                },
            },
        )


class FakeSecondSurfaceAdapter(PlatformAdapter):
    name = "wechat-lite"

    def get_profile(self) -> dict[str, object]:
        return {
            "adapter_name": self.name,
            "surface_family": "weixin",
            "transport_mode": "polling",
            "supports_mentions": False,
            "supports_attachments": True,
            "supports_replies": True,
            "supports_updates": False,
            "supports_live_receive": False,
        }

    def normalize_inbound(self, payload):  # type: ignore[no-untyped-def]
        return InboundMessage(
            platform="weixin",
            chat_id=str(payload.get("conversation_id") or "").strip(),
            user_id=str(payload.get("sender_id") or "").strip(),
            text=str(payload.get("body") or "").strip(),
            message_id=str(payload.get("msg_id") or "").strip(),
            chat_type=str(payload.get("conversation_type") or "group").strip().lower() or "group",
            route_key=str(payload.get("route_key") or "").strip(),
            session_id=str(payload.get("session_id") or "").strip(),
            attachments=[item for item in (payload.get("attachments") or []) if isinstance(item, dict)],
            metadata=dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {},
            raw_payload=payload,
        )

    def send_outbound(self, outbound: OutboundMessage):  # type: ignore[no-untyped-def]
        return {
            "platform": "weixin",
            "chat_id": outbound.chat_id,
            "text": outbound.text,
            "timestamp": outbound.timestamp,
            "sent": True,
            "message_id": "wechat-lite-message-1",
            "metadata": dict(outbound.metadata),
        }


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
            self.assertEqual(first["metadata"]["source"], "harborbeacon")
            self.assertEqual(first["metadata"]["resume_token"], "resume_first")
            self.assertEqual(second["metadata"]["task_id"], "task_second")
            self.assertNotIn("resume_token", store.load_metadata("feishu", "room-1"))

    def test_replayed_older_message_does_not_rewind_gateway_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_client = FakeReplayTaskClient()
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
            replay = gateway.handle_inbound(
                "webhook",
                {
                    "platform": "feishu",
                    "chat_id": "room-1",
                    "user_id": "alice",
                    "text": "scan camera",
                    "message_id": "msg-1",
                },
            )

            metadata = store.load_metadata("feishu", "room-1")
            self.assertEqual(first["metadata"]["task_id"], "task_first")
            self.assertEqual(second["metadata"]["task_id"], "task_second")
            self.assertEqual(replay["metadata"]["task_id"], "task_first")
            self.assertEqual(replay["metadata"]["resume_token"], "resume_first")
            self.assertEqual(metadata["last_task_id"], "task_second")
            self.assertEqual(metadata["last_trace_id"], "trace_second")
            self.assertEqual(metadata["last_message_id"], "msg-2")
            self.assertNotIn("resume_token", metadata)
            self.assertEqual(metadata["message_task_ids"]["msg-1"], "task_first")
            self.assertEqual(metadata["message_task_ids"]["msg-2"], "task_second")

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

    def test_observability_logs_cover_inbound_and_delivery_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_client = FakeTaskClient()
            store = FileSessionStore(tmp, max_turns=10)
            gateway = GatewayService(
                store=store,
                brain=RuleBasedBrain(),
                task_client=task_client,
            )
            gateway.register_adapter(FakeDeliveryAdapter())

            with self.assertLogs("im_agent.gateway", level="INFO") as logs:
                gateway.handle_inbound(
                    "webhook",
                    {
                        "platform": "feishu",
                        "chat_id": "room-1",
                        "user_id": "alice",
                        "text": "scan camera",
                        "message_id": "msg-1",
                    },
                )
                route_key = str(store.load_metadata("feishu", "room-1")["route_key"])
                gateway.handle_notification_delivery(self._notification_payload(route_key, idempotency_key="idem-logs"))

            joined = "\n".join(logs.output)
            self.assertIn('"event": "inbound_task_handled"', joined)
            self.assertIn('"task_id": "task_first"', joined)
            self.assertIn('"trace_id": "trace_first"', joined)
            self.assertIn('"route_key": "gw_route_room1"', joined)
            self.assertIn('"message_id": "msg-1"', joined)
            self.assertIn('"event": "delivery_attempted"', joined)
            self.assertIn('"notification_id": "notif-1"', joined)
            self.assertIn('"delivery.idempotency_key": "idem-logs"', joined)
            self.assertIn('"provider_message_id": "provider_msg_123"', joined)

    def test_retrieval_ingress_logs_attachment_summary_without_leaking_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
            )
            gateway.register_adapter(WebhookAdapter())

            payload = {
                "platform": "feishu",
                "chat_id": "room-retrieval",
                "user_id": "alice",
                "text": "find the design file",
                "message_id": "msg-retrieval-1",
                "attachments": [
                    {
                        "type": "file",
                        "file_key": "file_opaque_123",
                        "name": "design-spec.pdf",
                        "mime_type": "application/pdf",
                        "download_url": "https://files.example/private?token=secret",
                    }
                ],
                "metadata": {
                    "transport_hint": "opaque",
                },
            }

            with self.assertLogs("im_agent.gateway", level="INFO") as logs:
                response = gateway.handle_inbound("webhook", payload)

            joined = "\n".join(logs.output)
            self.assertIn('"event": "inbound_brain_reply"', joined)
            self.assertIn('"content_kind": "retrieval_candidate"', joined)
            self.assertIn('"attachment_count": 1', joined)
            self.assertIn('"attachment_types": ["file"]', joined)
            self.assertIn('"raw_text": "find the design file"', joined)
            self.assertIn('"message_id": "msg-retrieval-1"', joined)
            self.assertNotIn("file_opaque_123", joined)
            self.assertNotIn("design-spec.pdf", joined)
            self.assertNotIn("token=secret", joined)
            self.assertEqual(response["metadata"]["ingress_profile"]["content_kind"], "retrieval_candidate")
            self.assertEqual(response["metadata"]["ingress_profile"]["attachment_count"], 1)
            self.assertEqual(response["metadata"]["ingress_profile"]["attachment_types"], ["file"])
            self.assertEqual(response["metadata"]["ingress_profile"]["attachment_metadata_keys"], ["download_url", "file_key", "mime_type", "name", "type"])
            self.assertEqual(response["metadata"]["ingress_profile"]["raw_text"], "find the design file")
            self.assertTrue(str(response["metadata"]["ingress_profile"]["route_key"]).startswith("gw_route_"))
            self.assertTrue(str(response["metadata"]["ingress_profile"]["session_id"]).startswith("gw_sess_"))

    def test_task_client_path_preserves_retrieval_attachments_and_transport_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_client = FakeTaskClient()
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
                task_client=task_client,
            )
            gateway.register_adapter(WebhookAdapter())

            response = gateway.handle_inbound(
                "webhook",
                {
                    "platform": "feishu",
                    "chat_id": "room-retrieval",
                    "user_id": "alice",
                    "text": "find the design file",
                    "message_id": "msg-retrieval-2",
                    "attachments": [
                        {
                            "type": "file",
                            "file_key": "file_opaque_123",
                            "name": "design-spec.pdf",
                            "mime_type": "application/pdf",
                            "download_url": "https://files.example/private?token=secret",
                        }
                    ],
                    "metadata": {
                        "transport_hint": "opaque",
                    },
                },
            )

            inbound = task_client.calls[0]["incoming"]
            self.assertEqual(inbound.attachments[0]["file_key"], "file_opaque_123")
            self.assertEqual(inbound.attachments[0]["name"], "design-spec.pdf")
            self.assertEqual(inbound.metadata["transport_hint"], "opaque")
            self.assertEqual(response["metadata"]["ingress_profile"]["content_kind"], "retrieval_candidate")
            self.assertEqual(response["metadata"]["ingress_profile"]["attachment_count"], 1)
            self.assertEqual(response["metadata"]["ingress_profile"]["attachment_types"], ["file"])
            self.assertEqual(response["metadata"]["ingress_profile"]["attachment_metadata_keys"], ["download_url", "file_key", "mime_type", "name", "type"])
            self.assertNotIn("resume_token", task_client.calls[0]["session_metadata"])

    def test_retrieval_reply_renders_citations_and_artifacts_without_leaking_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_client = FakeRetrievalTaskClient()
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
                task_client=task_client,
            )
            gateway.register_adapter(WebhookAdapter())

            payload = {
                "platform": "feishu",
                "chat_id": "room-retrieval-reply",
                "user_id": "alice",
                "text": "find the latest policy with attached diagram",
                "message_id": "msg-retrieval-reply-1",
                "attachments": [
                    {
                        "type": "file",
                        "file_key": "file_opaque_456",
                        "name": "diagram.png",
                        "mime_type": "image/png",
                        "download_url": "https://files.example/private?token=secret",
                    }
                ],
            }

            with self.assertLogs("im_agent.gateway", level="INFO") as logs:
                response = gateway.handle_inbound("webhook", payload)

            joined = "\n".join(logs.output)
            self.assertIn('"event": "retrieval_reply_classified"', joined)
            self.assertIn('"event": "retrieval_reply_rendered"', joined)
            self.assertIn('"citation_count": 3', joined)
            self.assertIn('"artifact_count": 2', joined)
            self.assertIn('"rendered_sections": ["引用", "附件"]', joined)
            self.assertIn('"event": "inbound_task_handled"', joined)
            self.assertIn('"content_kind": "retrieval_candidate"', joined)
            self.assertIn('"retrieval_render_kind": "retrieval_reply"', joined)
            self.assertIn('"task_id": "task_retrieval"', joined)
            self.assertIn('"trace_id": "trace_retrieval"', joined)
            self.assertIn("检索结果（3 条引用，2 个附件）", response["text"])
            self.assertIn("引用", response["text"])
            self.assertIn("附件", response["text"])
            self.assertIn("Policy Index [kb-alpha]", response["text"])
            self.assertIn("diagram.png (image/png)", response["text"])
            self.assertEqual(response["metadata"]["retrieval_render"]["content_kind"], "retrieval_reply")
            self.assertEqual(response["metadata"]["retrieval_render"]["citation_count"], 3)
            self.assertEqual(response["metadata"]["retrieval_render"]["artifact_count"], 2)
            self.assertEqual(response["metadata"]["retrieval_render"]["rendered_sections"], ["引用", "附件"])
            self.assertNotIn("file_opaque_456", joined)
            self.assertNotIn("diagram.png", joined)
            self.assertNotIn("Policy Index", joined)
            self.assertNotIn("kb-alpha", joined)
            self.assertNotIn("report.pdf", joined)

    def test_retrieval_reply_degrades_to_plain_reply_when_no_reply_pack_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_client = FakePlainRetrievalTaskClient()
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
                task_client=task_client,
            )
            gateway.register_adapter(WebhookAdapter())

            with self.assertLogs("im_agent.gateway", level="INFO") as logs:
                response = gateway.handle_inbound(
                    "webhook",
                    {
                        "platform": "feishu",
                        "chat_id": "room-retrieval-plain",
                        "user_id": "alice",
                        "text": "find the latest policy",
                        "message_id": "msg-retrieval-plain-1",
                    },
                )

            joined = "\n".join(logs.output)
            self.assertIn('"event": "retrieval_reply_classified"', joined)
            self.assertNotIn('"event": "retrieval_reply_rendered"', joined)
            self.assertIn('"retrieval_render_kind": "plain_reply"', joined)
            self.assertIn("HarborBeacon returned a plain reply.", response["text"])
            self.assertEqual(response["metadata"]["retrieval_render"]["content_kind"], "plain_reply")
            self.assertEqual(response["metadata"]["retrieval_render"]["citation_count"], 0)
            self.assertEqual(response["metadata"]["retrieval_render"]["artifact_count"], 0)
            self.assertEqual(response["metadata"]["retrieval_render"]["rendered_sections"], [])

    def test_local_retrieval_round_trip_launch_pack_smoke(self) -> None:
        payload = {
            "platform": "feishu",
            "chat_id": "room-launch-pack",
            "user_id": "alice",
            "text": "find the latest policy with attached diagram",
            "attachments": [
                {
                    "type": "file",
                    "file_key": "file_opaque_launch_pack",
                    "name": "diagram.png",
                    "mime_type": "image/png",
                    "download_url": "https://files.example/private?token=secret",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as rich_tmp:
            rich_gateway = GatewayService(
                store=FileSessionStore(rich_tmp, max_turns=10),
                brain=RuleBasedBrain(),
                task_client=FakeRetrievalTaskClient(),
            )
            rich_gateway.register_adapter(WebhookAdapter())

            with self.assertLogs("im_agent.gateway", level="INFO") as rich_logs:
                rich_response = rich_gateway.handle_inbound(
                    "webhook",
                    {
                        **payload,
                        "message_id": "msg-launch-pack-rich",
                    },
                )

            rich_joined = "\n".join(rich_logs.output)
            self.assertIn('"event": "retrieval_reply_classified"', rich_joined)
            self.assertIn('"event": "retrieval_reply_rendered"', rich_joined)
            self.assertIn('"citation_count": 3', rich_joined)
            self.assertIn('"artifact_count": 2', rich_joined)
            self.assertIn("检索结果（3 条引用，2 个附件）", rich_response["text"])
            self.assertEqual(rich_response["metadata"]["retrieval_render"]["content_kind"], "retrieval_reply")
            self.assertEqual(rich_response["metadata"]["retrieval_render"]["citation_count"], 3)
            self.assertEqual(rich_response["metadata"]["retrieval_render"]["artifact_count"], 2)

        with tempfile.TemporaryDirectory() as plain_tmp:
            plain_gateway = GatewayService(
                store=FileSessionStore(plain_tmp, max_turns=10),
                brain=RuleBasedBrain(),
                task_client=FakePlainRetrievalTaskClient(),
            )
            plain_gateway.register_adapter(WebhookAdapter())

            with self.assertLogs("im_agent.gateway", level="INFO") as plain_logs:
                plain_response = plain_gateway.handle_inbound(
                    "webhook",
                    {
                        **payload,
                        "message_id": "msg-launch-pack-plain",
                    },
                )

            plain_joined = "\n".join(plain_logs.output)
            self.assertIn('"event": "retrieval_reply_classified"', plain_joined)
            self.assertNotIn('"event": "retrieval_reply_rendered"', plain_joined)
            self.assertIn('"retrieval_render_kind": "plain_reply"', plain_joined)
            self.assertIn("HarborBeacon returned a plain reply.", plain_response["text"])
            self.assertEqual(plain_response["metadata"]["retrieval_render"]["content_kind"], "plain_reply")
            self.assertEqual(plain_response["metadata"]["retrieval_render"]["citation_count"], 0)
            self.assertEqual(plain_response["metadata"]["retrieval_render"]["artifact_count"], 0)

    def test_second_surface_profile_shape_can_render_retrieval_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayService(
                store=FileSessionStore(tmp, max_turns=10),
                brain=RuleBasedBrain(),
                task_client=FakeRetrievalTaskClient(),
            )
            gateway.register_adapter(FakeSecondSurfaceAdapter())

            payload = {
                "conversation_id": "room-second-surface",
                "sender_id": "alice",
                "body": "find the latest policy with attached diagram",
                "msg_id": "msg-second-surface-1",
                "conversation_type": "group",
                "route_key": "surface_route_1",
                "session_id": "surface_sess_1",
                "attachments": [
                    {
                        "type": "file",
                        "name": "diagram.png",
                        "mime_type": "image/png",
                    }
                ],
                "metadata": {
                    "surface_hint": "wechat-like",
                },
            }

            with self.assertLogs("im_agent.gateway", level="INFO") as logs:
                response = gateway.handle_inbound("wechat-lite", payload)

            joined = "\n".join(logs.output)
            self.assertIn('"event": "retrieval_reply_classified"', joined)
            self.assertIn('"event": "retrieval_reply_rendered"', joined)
            self.assertIn('"surface_family": "weixin"', joined)
            self.assertIn('"transport_mode": "polling"', joined)
            self.assertEqual(response["metadata"]["adapter_profile"]["surface_family"], "weixin")
            self.assertEqual(response["metadata"]["adapter_profile"]["transport_mode"], "polling")
            self.assertEqual(response["metadata"]["ingress_profile"]["content_kind"], "retrieval_candidate")
            self.assertEqual(response["metadata"]["retrieval_render"]["content_kind"], "retrieval_reply")
            self.assertIn("检索结果（3 条引用，2 个附件）", response["text"])
            self.assertIn("diagram.png (image/png)", response["text"])


if __name__ == "__main__":
    unittest.main()

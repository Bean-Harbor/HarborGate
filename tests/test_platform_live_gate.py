import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from im_agent.models import InboundMessage, OutboundMessage
from im_agent.platforms.base import PlatformAdapter


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "run_platform_live_gate.py"
MODULE_SPEC = importlib.util.spec_from_file_location("run_platform_live_gate", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
platform_live_gate = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(platform_live_gate)


class FakeFeishuLiveAdapter(PlatformAdapter):
    name = "feishu"

    def get_profile(self) -> dict[str, object]:
        return {
            "adapter_name": self.name,
            "surface_family": "feishu",
            "transport_mode": "websocket",
            "supports_mentions": True,
            "supports_attachments": True,
            "supports_replies": True,
            "supports_updates": False,
            "supports_live_receive": True,
        }

    def normalize_inbound(self, payload: dict[str, object]) -> InboundMessage:
        return InboundMessage(
            platform="feishu",
            chat_id=str(payload.get("chat_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            text=str(payload.get("text") or ""),
            message_id=str(payload.get("message_id") or ""),
            chat_type=str(payload.get("chat_type") or "p2p"),
            raw_payload=payload,
        )

    def send_outbound(self, outbound: OutboundMessage) -> dict[str, object]:
        return {
            "platform": "feishu",
            "chat_id": outbound.chat_id,
            "text": outbound.text,
            "timestamp": outbound.timestamp,
            "sent": True,
            "message_id": f"provider_{outbound.metadata.get('task_id', 'plain')}",
            "metadata": dict(outbound.metadata),
        }


class HarborBeaconRehearsalHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        payload = json.loads(body) if body else {}

        intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        domain = str(intent.get("domain") or "")
        action = str(intent.get("action") or "")
        task_id = str(payload.get("task_id") or "task_unknown")
        trace_id = str(payload.get("trace_id") or "trace_unknown")

        if domain == "service" and action == "status":
            response_payload = {
                "task_id": task_id,
                "trace_id": trace_id,
                "status": "completed",
                "result": {
                    "message": "ssh is running.",
                    "data": {
                        "domain": "service",
                        "operation": "status",
                        "executor_used": "middleware_api",
                    },
                },
            }
        elif domain == "service" and action == "restart" and not str(args.get("approval_token") or "").strip():
            response_payload = {
                "task_id": task_id,
                "trace_id": trace_id,
                "status": "needs_input",
                "prompt": "restart requires approval",
                "resume_token": f"resume_{task_id}",
                "result": {
                    "message": "restart requires approval",
                    "next_actions": ["approval_token approved"],
                },
            }
        elif domain == "service" and action == "restart":
            response_payload = {
                "task_id": task_id,
                "trace_id": trace_id,
                "status": "completed",
                "result": {
                    "message": "ssh restarted.",
                    "data": {
                        "domain": "service",
                        "operation": "restart",
                        "executor_used": "middleware_api",
                    },
                },
            }
        elif domain == "files" and action == "list":
            response_payload = {
                "task_id": task_id,
                "trace_id": trace_id,
                "status": "completed",
                "result": {
                    "message": "listed /mnt",
                    "data": {
                        "domain": "files",
                        "operation": "list",
                        "executor_used": "middleware_api",
                    },
                },
            }
        else:
            response_payload = {
                "task_id": task_id,
                "trace_id": trace_id,
                "status": "failed",
                "result": {
                    "message": f"unsupported {domain}.{action}",
                },
            }

        encoded = json.dumps(response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class PlatformLiveGateTests(unittest.TestCase):
    def test_classify_weixin_blocker_marks_poll_timeout(self) -> None:
        blocker = platform_live_gate.classify_weixin_blocker(
            {
                "configured": True,
                "poll": {"status": "timeout", "error": "The read operation timed out"},
                "live_send": {"status": "sent"},
                "notification_replay": {"first_ok": True, "replay_identical": True},
            }
        )
        self.assertEqual(blocker, "weixin_poll_timeout")

    def test_classify_weixin_blocker_marks_dns_resolution(self) -> None:
        blocker = platform_live_gate.classify_weixin_blocker(
            {
                "configured": True,
                "poll": {
                    "status": "error",
                    "error": "<urlopen error [Errno 11001] getaddrinfo failed>",
                },
            }
        )
        self.assertEqual(blocker, "weixin_dns_resolution")

    def test_classify_weixin_blocker_distinguishes_waiting_for_private_text(self) -> None:
        blocker = platform_live_gate.classify_weixin_blocker(
            {
                "configured": True,
                "poll": {
                    "status": "ok",
                    "outcome": "idle_timeout",
                    "private_text_message_count": 0,
                },
                "live_send": {"status": "sent"},
                "notification_replay": {"first_ok": True, "replay_identical": True},
            }
        )
        self.assertEqual(blocker, "weixin_waiting_for_private_text")

    def test_classify_weixin_blocker_accepts_recent_probe_evidence(self) -> None:
        blocker = platform_live_gate.classify_weixin_blocker(
            {
                "configured": True,
                "poll": {
                    "status": "ok",
                    "outcome": "idle_timeout",
                    "private_text_message_count": 0,
                },
                "ingress_probe": {"provider_private_text_seen": True},
                "live_send": {"status": "sent"},
                "notification_replay": {"first_ok": True, "replay_identical": True},
            }
        )
        self.assertEqual(blocker, "")

    def test_classify_weixin_blocker_marks_dns_resolution_on_live_send_failure(self) -> None:
        blocker = platform_live_gate.classify_weixin_blocker(
            {
                "configured": True,
                "poll": {
                    "status": "ok",
                    "outcome": "messages",
                    "private_text_message_count": 1,
                },
                "ingress_probe": {"provider_private_text_seen": True},
                "live_send": {
                    "status": "error",
                    "error": "HTTPSConnectionPool(host='ilinkai.weixin.qq.com'): NameResolutionError",
                },
            }
        )
        self.assertEqual(blocker, "weixin_dns_resolution")

    def test_discover_latest_weixin_ingress_probe_prefers_current_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            probe_dir = runtime_dir / "weixin-ingress-probe"
            probe_dir.mkdir(parents=True, exist_ok=True)
            successful_probe = probe_dir / "weixin-ingress-probe-success.json"
            latest_probe = probe_dir / "weixin-ingress-probe-latest.json"
            successful_probe.write_text(
                json.dumps({"provider_private_text_seen": True, "blocked_reason": ""}),
                encoding="utf-8",
            )
            latest_probe.write_text(
                json.dumps(
                    {
                        "provider_private_text_seen": False,
                        "blocked_reason": "waiting_for_private_text",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(successful_probe, (1_700_000_000, 1_700_000_000))
            os.utime(latest_probe, (1_700_000_100, 1_700_000_100))

            current = platform_live_gate.discover_latest_weixin_ingress_probe(runtime_dir)
            successful = platform_live_gate.discover_latest_successful_weixin_ingress_probe(runtime_dir)

            self.assertFalse(current["provider_private_text_seen"])
            self.assertEqual(current["blocked_reason"], "waiting_for_private_text")
            self.assertTrue(successful["provider_private_text_seen"])
            self.assertEqual(successful["blocked_reason"], "")

    def test_discover_weixin_state_prefers_newest_account_and_matching_context_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            accounts_dir = Path(tmpdir) / "accounts"
            accounts_dir.mkdir(parents=True, exist_ok=True)
            old_account = accounts_dir / "26d88700bd27_im.bot.json"
            new_account = accounts_dir / "d5ba3cf20a24_im.bot.json"
            old_context = accounts_dir / "26d88700bd27_im.bot.context_tokens.json"
            new_context = accounts_dir / "d5ba3cf20a24_im.bot.context_tokens.json"
            old_runtime = accounts_dir / "26d88700bd27_im.bot.runtime.json"

            old_account.write_text(json.dumps({"account_id": "old@im.bot"}), encoding="utf-8")
            new_account.write_text(json.dumps({"account_id": "new@im.bot"}), encoding="utf-8")
            old_context.write_text(json.dumps({"old-chat": "old-token"}), encoding="utf-8")
            new_context.write_text(json.dumps({"new-chat": "new-token"}), encoding="utf-8")
            old_runtime.write_text(json.dumps({"status": "stale"}), encoding="utf-8")

            os.utime(old_account, (1_700_000_000, 1_700_000_000))
            os.utime(new_account, (1_700_000_100, 1_700_000_100))
            os.utime(old_context, (1_700_000_000, 1_700_000_000))
            os.utime(new_context, (1_700_000_100, 1_700_000_100))
            os.utime(old_runtime, (1_700_000_200, 1_700_000_200))

            state = platform_live_gate.discover_weixin_state(Path(tmpdir))

            self.assertEqual(state["account"]["account_id"], "new@im.bot")
            self.assertEqual(state["context_tokens"], {"new-chat": "new-token"})

    def test_classify_weixin_ingress_blocker_groups_parity_checks(self) -> None:
        self.assertEqual(
            platform_live_gate.classify_weixin_ingress_blocker({"configured": False}),
            "account_restore",
        )
        self.assertEqual(
            platform_live_gate.classify_weixin_ingress_blocker(
                {
                    "configured": True,
                    "poll": {"status": "error", "error": "HTTP 403 forbidden"},
                }
            ),
            "qr_recovery",
        )
        self.assertEqual(
            platform_live_gate.classify_weixin_ingress_blocker(
                {
                    "configured": True,
                    "poll": {"status": "timeout", "error": "The read operation timed out"},
                }
            ),
            "getupdates",
        )
        self.assertEqual(
            platform_live_gate.classify_weixin_ingress_blocker(
                {
                    "configured": True,
                    "poll": {"status": "error", "error": "<urlopen error [Errno 11001] getaddrinfo failed>"},
                }
            ),
            "getupdates",
        )
        self.assertEqual(
            platform_live_gate.classify_weixin_ingress_blocker(
                {
                    "configured": True,
                    "poll": {"status": "ok", "private_text_message_count": 0},
                }
            ),
            "getupdates",
        )
        self.assertEqual(
            platform_live_gate.classify_weixin_ingress_blocker(
                {
                    "configured": True,
                    "poll": {"status": "ok", "private_text_message_count": 0},
                    "ingress_probe": {"provider_private_text_seen": True},
                    "live_send": {"status": "sent"},
                }
            ),
            "",
        )
        self.assertEqual(
            platform_live_gate.classify_weixin_ingress_blocker(
                {
                    "configured": True,
                    "poll": {"status": "ok", "private_text_message_count": 1},
                    "live_send": {"status": "error"},
                }
            ),
            "context_token_send",
        )

    def test_summarize_decision_uses_feishu_baseline_when_weixin_parity_is_pending(self) -> None:
        report = {
            "task_api_url_present": True,
            "weixin": {
                "rehearsal_ready": False,
                "blocked_reason": "weixin_waiting_for_private_text",
                "blocker_category": "weixin_waiting_for_private_text",
                "ingress_blocker_category": "getupdates",
            },
            "feishu": {
                "rehearsal_ready": True,
                "rollback_ready": True,
            },
        }

        platform_live_gate.summarize_decision(report)

        self.assertEqual(report["decision"], "feishu_baseline_with_weixin_parity_track")
        self.assertFalse(report["parity_ready"])
        self.assertTrue(report["feishu"]["rehearsal_ready"])
        self.assertFalse(report["weixin"]["rehearsal_ready"])
        self.assertEqual(report["weixin_blocker_category"], "getupdates")
        self.assertEqual(report["decision_reason"], "getupdates")

    def test_summarize_decision_blocks_when_task_api_rehearsal_is_missing(self) -> None:
        report = {
            "task_api_url_present": True,
            "weixin": {
                "rehearsal_ready": False,
                "blocked_reason": "weixin_harborbeacon_rehearsal_failed",
                "blocker_category": "weixin_harborbeacon_rehearsal_failed",
            },
            "feishu": {
                "rehearsal_ready": False,
                "rollback_ready": True,
                "blocked_reason": "feishu_harborbeacon_rehearsal_failed",
            },
        }

        platform_live_gate.summarize_decision(report)

        self.assertEqual(report["decision"], "blocked")
        self.assertIn("feishu_harborbeacon_rehearsal_failed", report["decision_reason"])

    def test_run_feishu_task_rehearsal_reports_rehearsal_ready(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), HarborBeaconRehearsalHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = platform_live_gate.run_feishu_task_rehearsal(
                FakeFeishuLiveAdapter(),
                chat_id="oc_demo_chat",
                user_id="ou_demo_user",
                task_api_url=f"http://127.0.0.1:{server.server_port}",
                task_api_token="",
            )

            self.assertTrue(result["ran"])
            self.assertTrue(result["rehearsal_ready"])
            self.assertTrue(result["route_key_present"])
            self.assertTrue(result["restart_turn"]["resume_token_present"])
            self.assertEqual(result["status_turn"]["status"], "completed")
            self.assertEqual(result["restart_turn"]["status"], "needs_input")
            self.assertEqual(result["resume_turn"]["status"], "completed")
            self.assertEqual(result["files_turn"]["status"], "completed")
            self.assertTrue(result["notification_replay"]["first_ok"])
            self.assertTrue(result["notification_replay"]["replay_identical"])
            self.assertEqual(result["notification_replay"]["route_mode"], "source_bound")
            self.assertEqual(result["notification_replay"]["queue_state"], "complete")
            self.assertTrue(result["proactive_notification_replay"]["first_ok"])
            self.assertTrue(result["proactive_notification_replay"]["replay_identical"])
            self.assertEqual(result["proactive_notification_replay"]["route_mode"], "proactive")
            self.assertEqual(result["proactive_notification_replay"]["queue_state"], "complete")
            self.assertEqual(result["delivery_observability"]["route_mode_counts"]["source_bound"], 1)
            self.assertEqual(result["delivery_observability"]["route_mode_counts"]["proactive"], 1)
            self.assertEqual(result["delivery_observability"]["route_mode_breakdown"]["source_bound"]["queue_state_counts"]["complete"], 1)
            self.assertEqual(result["delivery_observability"]["route_mode_breakdown"]["proactive"]["queue_state_counts"]["complete"], 1)
            self.assertIn("delivery_health", result)
            self.assertTrue(result["delivery_health"]["source_bound"]["ready"])
            self.assertTrue(result["delivery_health"]["proactive"]["ready"])
            self.assertTrue(result["replay_task_id_matches"])
            self.assertTrue(result["session_pointer_preserved"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_summarize_decision_marks_dual_surface_ready_when_both_surfaces_pass(self) -> None:
        report = {
            "task_api_url_present": True,
            "weixin": {"rehearsal_ready": True},
            "feishu": {"rehearsal_ready": True, "rollback_ready": True},
        }

        platform_live_gate.summarize_decision(report)

        self.assertEqual(report["decision"], "dual_surface_ready")
        self.assertTrue(report["parity_ready"])
        self.assertEqual(report["decision_reason"], "feishu_and_weixin_rehearsal_ready")
        self.assertIn("release_v1", report)
        self.assertEqual(report["release_v1"]["delivery_policy"]["interactive_reply"], "source_bound")
        self.assertTrue(report["release_v1"]["dual_surface_ready"])


if __name__ == "__main__":
    unittest.main()

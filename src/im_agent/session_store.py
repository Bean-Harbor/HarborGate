from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from im_agent.models import ConversationTurn


class FileSessionStore:
    """Persist per-chat history in JSON files."""

    def __init__(self, root: str | Path, max_turns: int = 20) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_turns = max_turns
        self._lock = threading.Lock()
        self._route_path = self.root / "_routes.json"
        self._delivery_path = self.root / "_deliveries.json"

    def load_history(self, platform: str, chat_id: str) -> list[ConversationTurn]:
        payload = self._load_payload(platform, chat_id)
        turns = payload.get("turns", [])
        return [ConversationTurn.from_dict(item) for item in turns]

    def load_metadata(self, platform: str, chat_id: str) -> dict[str, object]:
        payload = self._load_payload(platform, chat_id)
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            return {}
        return dict(metadata)

    def append_turns(
        self,
        platform: str,
        chat_id: str,
        turns: list[ConversationTurn],
    ) -> list[ConversationTurn]:
        with self._lock:
            payload = self._load_payload(platform, chat_id)
            turns_payload = payload.get("turns", [])
            history = [ConversationTurn.from_dict(item) for item in turns_payload]
            history.extend(turns)
            if self.max_turns > 0:
                history = history[-self.max_turns :]
            next_payload = {
                "platform": platform,
                "chat_id": chat_id,
                "metadata": payload.get("metadata", {}),
                "turns": [turn.to_dict() for turn in history],
            }
            path = self._session_path(platform, chat_id)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(next_payload, handle, ensure_ascii=False, indent=2)
            return history

    def set_metadata(self, platform: str, chat_id: str, metadata: dict[str, object]) -> dict[str, object]:
        with self._lock:
            payload = self._load_payload(platform, chat_id)
            payload["platform"] = platform
            payload["chat_id"] = chat_id
            payload["metadata"] = dict(metadata)
            payload.setdefault("turns", [])
            path = self._session_path(platform, chat_id)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            return dict(payload["metadata"])

    def register_route(self, route_key: str, route: dict[str, object]) -> dict[str, object]:
        if not route_key:
            raise ValueError("route_key is required")
        with self._lock:
            routes = self._load_shared_map(self._route_path)
            routes[route_key] = dict(route)
            self._write_shared_map(self._route_path, routes)
            return dict(routes[route_key])

    def resolve_route(self, route_key: str) -> dict[str, object] | None:
        if not route_key:
            return None
        with self._lock:
            routes = self._load_shared_map(self._route_path)
            route = routes.get(route_key)
            return dict(route) if isinstance(route, dict) else None

    def load_delivery_record(self, idempotency_key: str) -> dict[str, object] | None:
        if not idempotency_key:
            return None
        with self._lock:
            records = self._load_shared_map(self._delivery_path)
            record = records.get(idempotency_key)
            return dict(record) if isinstance(record, dict) else None

    def load_delivery_records(self) -> dict[str, dict[str, object]]:
        with self._lock:
            records = self._load_shared_map(self._delivery_path)
            return {
                key: dict(value)
                for key, value in records.items()
                if isinstance(key, str) and isinstance(value, dict)
            }

    @staticmethod
    def _route_mode_summary_template() -> dict[str, object]:
        return {
            "record_count": 0,
            "sent_count": 0,
            "failed_count": 0,
            "retryable_failure_count": 0,
            "terminal_failure_count": 0,
            "queue_state_counts": {
                "complete": 0,
                "retry_queue": 0,
                "terminal_failure": 0,
                "unknown": 0,
            },
            "failure_class_counts": {},
        }

    def summarize_delivery_records(self) -> dict[str, object]:
        records = self.load_delivery_records()
        summary: dict[str, object] = {
            "record_count": len(records),
            "source_bound_count": 0,
            "proactive_count": 0,
            "sent_count": 0,
            "failed_count": 0,
            "retryable_failure_count": 0,
            "terminal_failure_count": 0,
            "queue_state_counts": {
                "complete": 0,
                "retry_queue": 0,
                "terminal_failure": 0,
                "unknown": 0,
            },
            "failure_class_counts": {},
            "route_mode_counts": {
                "source_bound": 0,
                "proactive": 0,
                "unknown": 0,
            },
            "route_mode_breakdown": {
                "source_bound": self._route_mode_summary_template(),
                "proactive": self._route_mode_summary_template(),
                "unknown": self._route_mode_summary_template(),
            },
            "recent_deliveries": [],
        }

        failure_class_counts: dict[str, int] = {}
        recent_deliveries: list[dict[str, object]] = []
        route_mode_breakdown = summary["route_mode_breakdown"]
        for idempotency_key, record in records.items():
            classification = record.get("classification")
            classification = classification if isinstance(classification, dict) else {}
            response_payload = record.get("response_payload")
            response_payload = response_payload if isinstance(response_payload, dict) else {}
            route_mode = str(classification.get("route_mode") or "unknown").strip().lower() or "unknown"
            outcome = str(classification.get("outcome") or "").strip().lower()
            if not outcome:
                outcome = "sent" if bool(response_payload.get("ok")) else "failed"
            failure_class = str(classification.get("failure_class") or "").strip()
            if not failure_class:
                error_block = response_payload.get("error")
                error_block = error_block if isinstance(error_block, dict) else {}
                failure_class = str(error_block.get("code") or "").strip()
            retryable = bool(classification.get("retryable") if "retryable" in classification else response_payload.get("retryable"))
            queue_state = str(classification.get("queue_state") or "").strip().lower()
            if not queue_state:
                if outcome == "sent":
                    queue_state = "complete"
                elif retryable:
                    queue_state = "retry_queue"
                elif outcome == "failed":
                    queue_state = "terminal_failure"
                else:
                    queue_state = "unknown"

            route_mode_bucket = route_mode if route_mode in {"source_bound", "proactive"} else "unknown"
            route_mode_summary = route_mode_breakdown[route_mode_bucket]

            if route_mode in {"source_bound", "proactive"}:
                summary[f"{route_mode}_count"] = int(summary.get(f"{route_mode}_count", 0) or 0) + 1
                summary["route_mode_counts"][route_mode] = int(summary["route_mode_counts"].get(route_mode, 0) or 0) + 1  # type: ignore[index]
            else:
                summary["route_mode_counts"]["unknown"] = int(summary["route_mode_counts"].get("unknown", 0) or 0) + 1  # type: ignore[index]

            route_mode_summary["record_count"] = int(route_mode_summary.get("record_count", 0) or 0) + 1
            if outcome == "sent":
                summary["sent_count"] = int(summary.get("sent_count", 0) or 0) + 1
                route_mode_summary["sent_count"] = int(route_mode_summary.get("sent_count", 0) or 0) + 1
            else:
                summary["failed_count"] = int(summary.get("failed_count", 0) or 0) + 1
                route_mode_summary["failed_count"] = int(route_mode_summary.get("failed_count", 0) or 0) + 1
                if retryable:
                    summary["retryable_failure_count"] = int(summary.get("retryable_failure_count", 0) or 0) + 1
                    route_mode_summary["retryable_failure_count"] = int(route_mode_summary.get("retryable_failure_count", 0) or 0) + 1
                else:
                    summary["terminal_failure_count"] = int(summary.get("terminal_failure_count", 0) or 0) + 1
                    route_mode_summary["terminal_failure_count"] = int(route_mode_summary.get("terminal_failure_count", 0) or 0) + 1

            queue_counts = summary["queue_state_counts"]
            queue_counts[queue_state] = int(queue_counts.get(queue_state, 0) or 0) + 1  # type: ignore[index]
            route_queue_counts = route_mode_summary["queue_state_counts"]
            route_queue_counts[queue_state] = int(route_queue_counts.get(queue_state, 0) or 0) + 1  # type: ignore[index]

            if failure_class:
                failure_class_counts[failure_class] = failure_class_counts.get(failure_class, 0) + 1
                route_failure_counts = route_mode_summary["failure_class_counts"]
                route_failure_counts[failure_class] = route_failure_counts.get(failure_class, 0) + 1

            recent_deliveries.append(
                {
                    "idempotency_key": idempotency_key,
                    "notification_id": str(response_payload.get("notification_id") or ""),
                    "route_mode": route_mode,
                    "outcome": outcome,
                    "failure_class": failure_class,
                    "retryable": retryable,
                    "queue_state": queue_state,
                }
            )

        summary["failure_class_counts"] = failure_class_counts
        summary["recent_deliveries"] = recent_deliveries[-5:]
        return summary

    @staticmethod
    def _route_mode_health_state(route_summary: dict[str, object]) -> dict[str, object]:
        record_count = int(route_summary.get("record_count", 0) or 0)
        sent_count = int(route_summary.get("sent_count", 0) or 0)
        failed_count = int(route_summary.get("failed_count", 0) or 0)
        retryable_failure_count = int(route_summary.get("retryable_failure_count", 0) or 0)
        terminal_failure_count = int(route_summary.get("terminal_failure_count", 0) or 0)
        queue_state_counts = route_summary.get("queue_state_counts")
        queue_state_counts = queue_state_counts if isinstance(queue_state_counts, dict) else {}
        failure_class_counts = route_summary.get("failure_class_counts")
        failure_class_counts = failure_class_counts if isinstance(failure_class_counts, dict) else {}

        if record_count <= 0:
            health_state = "unknown"
            health_note = "no_delivery_records"
        elif terminal_failure_count > 0:
            health_state = "blocked"
            health_note = "terminal_failures_present"
        elif retryable_failure_count > 0:
            health_state = "degraded"
            health_note = "retryable_failures_present"
        elif sent_count > 0:
            health_state = "ready"
            health_note = "all_attempts_sent"
        else:
            health_state = "degraded"
            health_note = "attempts_recorded_without_success"

        return {
            "record_count": record_count,
            "sent_count": sent_count,
            "failed_count": failed_count,
            "retryable_failure_count": retryable_failure_count,
            "terminal_failure_count": terminal_failure_count,
            "queue_state_counts": dict(queue_state_counts),
            "failure_class_counts": dict(failure_class_counts),
            "health_state": health_state,
            "health_note": health_note,
            "ready": health_state == "ready",
        }

    def summarize_delivery_health(self) -> dict[str, object]:
        delivery_summary = self.summarize_delivery_records()
        route_breakdown = delivery_summary.get("route_mode_breakdown")
        route_breakdown = route_breakdown if isinstance(route_breakdown, dict) else {}
        source_bound = route_breakdown.get("source_bound")
        source_bound = source_bound if isinstance(source_bound, dict) else {}
        proactive = route_breakdown.get("proactive")
        proactive = proactive if isinstance(proactive, dict) else {}
        unknown = route_breakdown.get("unknown")
        unknown = unknown if isinstance(unknown, dict) else {}
        return {
            "record_count": int(delivery_summary.get("record_count", 0) or 0),
            "route_mode_counts": dict(delivery_summary.get("route_mode_counts") or {}),
            "queue_state_counts": dict(delivery_summary.get("queue_state_counts") or {}),
            "source_bound": self._route_mode_health_state(source_bound),
            "proactive": self._route_mode_health_state(proactive),
            "unknown": self._route_mode_health_state(unknown),
            "failure_class_counts": dict(delivery_summary.get("failure_class_counts") or {}),
        }

    def save_delivery_record(
        self,
        idempotency_key: str,
        *,
        request_fingerprint: str,
        response_payload: dict[str, object],
        classification: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        with self._lock:
            records = self._load_shared_map(self._delivery_path)
            records[idempotency_key] = {
                "request_fingerprint": request_fingerprint,
                "response_payload": dict(response_payload),
                "classification": dict(classification or {}),
            }
            self._write_shared_map(self._delivery_path, records)
            return dict(records[idempotency_key])

    def _load_payload(self, platform: str, chat_id: str) -> dict[str, object]:
        path = self._session_path(platform, chat_id)
        if not path.exists():
            return {"platform": platform, "chat_id": chat_id, "metadata": {}, "turns": []}
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return {"platform": platform, "chat_id": chat_id, "metadata": {}, "turns": []}
        payload.setdefault("platform", platform)
        payload.setdefault("chat_id", chat_id)
        payload.setdefault("metadata", {})
        payload.setdefault("turns", [])
        return payload

    def _load_shared_map(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write_shared_map(path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def _session_path(self, platform: str, chat_id: str) -> Path:
        safe_platform = self._slug(platform)
        safe_chat = self._slug(chat_id)
        return self.root / f"{safe_platform}__{safe_chat}.json"

    @staticmethod
    def _slug(value: str) -> str:
        text = str(value).strip().lower() or "unknown"
        return re.sub(r"[^a-z0-9._-]+", "_", text)

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

    def save_delivery_record(
        self,
        idempotency_key: str,
        *,
        request_fingerprint: str,
        response_payload: dict[str, object],
    ) -> dict[str, object]:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        with self._lock:
            records = self._load_shared_map(self._delivery_path)
            records[idempotency_key] = {
                "request_fingerprint": request_fingerprint,
                "response_payload": dict(response_payload),
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

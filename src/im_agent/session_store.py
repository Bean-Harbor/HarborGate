from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from im_agent.models import ConversationTurn


class FileSessionStore:
    """Persist per-chat history in JSON files."""

    def __init__(self, root: str | Path, max_turns: int = 20) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_turns = max_turns
        self._lock = threading.Lock()

    def load_history(self, platform: str, chat_id: str) -> list[ConversationTurn]:
        path = self._session_path(platform, chat_id)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        turns = payload.get("turns", [])
        return [ConversationTurn.from_dict(item) for item in turns]

    def append_turns(
        self,
        platform: str,
        chat_id: str,
        turns: list[ConversationTurn],
    ) -> list[ConversationTurn]:
        with self._lock:
            history = self.load_history(platform, chat_id)
            history.extend(turns)
            if self.max_turns > 0:
                history = history[-self.max_turns :]
            payload = {
                "platform": platform,
                "chat_id": chat_id,
                "turns": [turn.to_dict() for turn in history],
            }
            path = self._session_path(platform, chat_id)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            return history

    def _session_path(self, platform: str, chat_id: str) -> Path:
        safe_platform = self._slug(platform)
        safe_chat = self._slug(chat_id)
        return self.root / f"{safe_platform}__{safe_chat}.json"

    @staticmethod
    def _slug(value: str) -> str:
        text = str(value).strip().lower() or "unknown"
        return re.sub(r"[^a-z0-9._-]+", "_", text)

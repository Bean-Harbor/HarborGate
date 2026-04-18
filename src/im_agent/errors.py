from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class GatewayContractError(Exception):
    status_code: int
    code: str
    message: str
    trace_id: str = ""

    def __str__(self) -> str:
        return self.message

    def to_response(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.trace_id:
            payload["trace_id"] = self.trace_id
        return payload

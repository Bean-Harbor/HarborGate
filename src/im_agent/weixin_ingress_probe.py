from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from im_agent.platforms.weixin import WeixinAdapter


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _mask(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}***{text[-4:]}"


def discover_account_id(state_dir: Path) -> str:
    accounts_dir = state_dir / "accounts"
    if not accounts_dir.exists():
        return ""
    ignored_suffixes = (".sync.json", ".context_tokens.json", ".processed_messages.json")
    for path in sorted(accounts_dir.glob("*.json")):
        if any(path.name.endswith(suffix) for suffix in ignored_suffixes):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            account_id = str(payload.get("account_id") or "").strip()
            if account_id:
                return account_id
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe real Weixin provider-side ingress and report whether a private text message was observed.",
    )
    parser.add_argument(
        "--state-dir",
        default="data/weixin",
        help="Directory containing saved Weixin account state.",
    )
    parser.add_argument(
        "--account-id",
        default="",
        help="Optional account id. If omitted, the probe auto-discovers the saved account.",
    )
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=90,
        help="Total probe window to watch for inbound provider-originated private text.",
    )
    parser.add_argument(
        "--poll-timeout-ms",
        type=int,
        default=35_000,
        help="Per-poll timeout. Idle long-poll timeouts are treated as healthy idle polls.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional JSON report output path. Defaults to data/runtime/weixin-ingress-probe/<timestamp>.json.",
    )
    return parser.parse_args()


def build_report(
    adapter: WeixinAdapter,
    *,
    started_at: str,
    polls_attempted: int,
    private_messages: list[dict[str, Any]],
    idle_polls: int,
    message_polls: int,
) -> dict[str, Any]:
    transport = adapter.transport_status()
    private_message_ids = [
        str(item.get("msg_id") or item.get("client_id") or "").strip()
        for item in private_messages
        if isinstance(item, dict)
    ]
    from_user_ids = sorted(
        {
            str(item.get("from_user_id") or "").strip()
            for item in private_messages
            if isinstance(item, dict) and str(item.get("from_user_id") or "").strip()
        }
    )
    provider_private_text_seen = bool(private_messages)
    blocked_reason = "" if provider_private_text_seen else "waiting_for_private_text"
    return {
        "generated_at": utc_now(),
        "started_at": started_at,
        "finished_at": utc_now(),
        "configured": bool(adapter.configured),
        "account_id_masked": _mask(str(getattr(adapter, "account_id", "") or "")),
        "window_seconds": None,
        "polls_attempted": polls_attempted,
        "idle_polls": idle_polls,
        "message_polls": message_polls,
        "provider_private_text_seen": provider_private_text_seen,
        "private_text_message_count": len(private_messages),
        "private_message_ids": [item for item in private_message_ids if item],
        "from_user_ids": from_user_ids,
        "blocked_reason": blocked_reason,
        "transport": {
            "status": str(transport.get("status") or "").strip(),
            "connected": bool(transport.get("connected")),
            "last_poll_outcome": str(transport.get("last_poll_outcome") or "").strip(),
            "last_poll_at": str(transport.get("last_poll_at") or "").strip(),
            "last_getupdates_at": str(transport.get("last_getupdates_at") or "").strip(),
            "last_getupdates_count": int(transport.get("last_getupdates_count") or 0),
            "last_private_text_message_count": int(transport.get("last_private_text_message_count") or 0),
            "last_getupdates_private_message_ids": list(transport.get("last_getupdates_private_message_ids") or []),
            "last_getupdates_error": str(transport.get("last_getupdates_error") or "").strip(),
            "last_inbound_at": str(transport.get("last_inbound_at") or "").strip(),
            "last_inbound_message_id": str(transport.get("last_inbound_message_id") or "").strip(),
            "last_inbound_chat_id": str(transport.get("last_inbound_chat_id") or "").strip(),
        },
        "next_action": (
            "send_one_private_text_message_from_real_weixin_client"
            if not provider_private_text_seen
            else "provider_private_text_confirmed"
        ),
    }


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir)
    account_id = str(args.account_id or "").strip() or discover_account_id(state_dir)
    adapter = WeixinAdapter(state_dir=state_dir, account_id=account_id or None)
    if not adapter.configured:
        print(
            json.dumps(
                {
                    "generated_at": utc_now(),
                    "configured": False,
                    "blocked_reason": "account_restore",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    started_at = utc_now()
    deadline = time.time() + max(1, int(args.window_seconds))
    polls_attempted = 0
    idle_polls = 0
    message_polls = 0
    private_messages: list[dict[str, Any]] = []

    while time.time() < deadline:
        polls_attempted += 1
        messages = adapter.poll_updates(timeout_ms=args.poll_timeout_ms)
        if messages:
            message_polls += 1
        else:
            idle_polls += 1
        private_messages.extend(
            item
            for item in messages
            if isinstance(item, dict)
            and not str(item.get("room_id") or "").strip()
            and any(
                isinstance(entry, dict) and int(entry.get("type") or 0) == 1
                for entry in (item.get("item_list") or [])
            )
        )
        if private_messages:
            break

    report = build_report(
        adapter,
        started_at=started_at,
        polls_attempted=polls_attempted,
        private_messages=private_messages,
        idle_polls=idle_polls,
        message_polls=message_polls,
    )
    report["window_seconds"] = max(1, int(args.window_seconds))

    output_path = Path(args.output) if str(args.output or "").strip() else (
        Path("data") / "runtime" / "weixin-ingress-probe" / f"weixin-ingress-probe-{slug_now()}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["provider_private_text_seen"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

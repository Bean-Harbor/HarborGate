from __future__ import annotations

import logging
import os
import time
from threading import Event

from im_agent.gateway import build_default_gateway
from im_agent.platforms.weixin import WeixinAdapter, discover_weixin_account

logging.basicConfig(
    level=os.getenv("IM_AGENT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def _stop_requested(stop_event: Event | None) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def _build_weixin_gateway_adapter(*, data_root: str | None = None, gateway=None):
    if gateway is None:
        gateway = build_default_gateway(data_root=data_root or os.getenv("IM_AGENT_DATA_DIR", "data/sessions"))
    return gateway, gateway.get_adapter("weixin")


def _wait_for_configured_weixin_adapter(
    *,
    stop_event: Event | None = None,
    data_root: str | None = None,
    sleep_fn=time.sleep,
    gateway=None,
):
    gateway, adapter = _build_weixin_gateway_adapter(data_root=data_root, gateway=gateway)
    while not isinstance(adapter, WeixinAdapter) or not adapter.configured:
        if _stop_requested(stop_event):
            return gateway, adapter
        logger.info("Weixin adapter is waiting for QR login credentials")
        sleep_fn(5)
        gateway, adapter = _build_weixin_gateway_adapter(data_root=data_root, gateway=gateway)
    return gateway, adapter


def _saved_account_id(adapter: WeixinAdapter) -> str:
    state_dir = getattr(adapter, "state_dir", "")
    if not state_dir:
        return ""
    saved = discover_weixin_account(state_dir)
    if not isinstance(saved, dict):
        return ""
    return str(saved.get("account_id") or "").strip()


def _current_account_is_still_saved(adapter: WeixinAdapter) -> bool:
    state_dir = getattr(adapter, "state_dir", "")
    account_id = str(getattr(adapter, "account_id", "") or "").strip()
    if not state_dir or not account_id:
        return bool(getattr(adapter, "configured", False))
    return discover_weixin_account(state_dir, account_id) is not None


def _weixin_adapter_should_reload(adapter) -> bool:
    if not isinstance(adapter, WeixinAdapter) or not adapter.configured:
        return True
    latest_account_id = _saved_account_id(adapter)
    current_account_id = str(getattr(adapter, "account_id", "") or "").strip()
    if latest_account_id and latest_account_id != current_account_id:
        return True
    if not latest_account_id and not _current_account_is_still_saved(adapter):
        return True
    return False


def run_loop(
    *,
    stop_event: Event | None = None,
    data_root: str | None = None,
    sleep_fn=time.sleep,
    gateway=None,
) -> None:
    gateway, adapter = _wait_for_configured_weixin_adapter(
        stop_event=stop_event,
        data_root=data_root,
        sleep_fn=sleep_fn,
        gateway=gateway,
    )
    if _stop_requested(stop_event) or not isinstance(adapter, WeixinAdapter) or not adapter.configured:
        return

    logger.info("Starting Weixin runner for account %s", adapter.account_id)

    while not _stop_requested(stop_event):
        if _weixin_adapter_should_reload(adapter):
            logger.info("Weixin runner detected credential state change; reloading adapter")
            gateway, adapter = _wait_for_configured_weixin_adapter(
                stop_event=stop_event,
                data_root=data_root,
                sleep_fn=sleep_fn,
                gateway=gateway,
            )
            if _stop_requested(stop_event) or not isinstance(adapter, WeixinAdapter) or not adapter.configured:
                return
            logger.info("Starting Weixin runner for account %s", adapter.account_id)
            continue
        try:
            messages = adapter.poll_updates()
            for payload in messages:
                if adapter.is_duplicate_update(payload):
                    logger.info("Skipping duplicate Weixin update %s", payload.get("msg_id") or payload.get("client_id"))
                    continue
                try:
                    gateway.handle_inbound("weixin", payload)
                    adapter.mark_update_processed(payload)
                except ValueError as exc:
                    logger.info("Skipping unsupported Weixin update: %s", exc)
        except KeyboardInterrupt:
            logger.info("Stopping Weixin runner")
            return
        except Exception as exc:
            logger.exception("Weixin runner loop failed: %s", exc)
            sleep_fn(3)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()

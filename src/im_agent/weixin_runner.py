from __future__ import annotations

import logging
import os
import time

from im_agent.gateway import build_default_gateway
from im_agent.platforms.weixin import WeixinAdapter

logging.basicConfig(
    level=os.getenv("IM_AGENT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    gateway = build_default_gateway(data_root=os.getenv("IM_AGENT_DATA_DIR", "data/sessions"))
    adapter = gateway.get_adapter("weixin")
    if not isinstance(adapter, WeixinAdapter):
        raise SystemExit(
            "Weixin adapter is not configured. Run harborgate-weixin-login first, then set WEIXIN_ACCOUNT_ID."
        )

    adapter.assert_configured()
    logger.info("Starting Weixin runner for account %s", adapter.account_id)

    while True:
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
            time.sleep(3)


if __name__ == "__main__":
    main()

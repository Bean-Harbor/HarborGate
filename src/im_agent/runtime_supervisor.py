from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from im_agent.gateway import GatewayService
from im_agent import weixin_runner

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off", "disabled"}


class GatewayRuntimeSupervisor:
    """Runs platform receive runtimes inside the harborgate service process."""

    def __init__(
        self,
        gateway: GatewayService,
        *,
        data_root: str,
        weixin_enabled: bool | None = None,
    ) -> None:
        self.gateway = gateway
        self.data_root = data_root
        self.weixin_enabled = _env_flag("HARBORGATE_WEIXIN_RUNTIME_ENABLED", True) if weixin_enabled is None else weixin_enabled
        self._stop_event = threading.Event()
        self._weixin_thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self.gateway.start()
        if self.weixin_enabled:
            self._weixin_thread = threading.Thread(
                target=self._run_weixin_loop,
                daemon=True,
                name="harborgate-weixin-runtime",
            )
            self._weixin_thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        if not self._started:
            return
        self._stop_event.set()
        thread = self._weixin_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_seconds)
        self._weixin_thread = None
        self.gateway.stop()
        self._started = False

    def status(self) -> dict[str, Any]:
        thread = self._weixin_thread
        return {
            "process_runtime": "harborgate.service",
            "started": self._started,
            "weixin": {
                "mode": "in_process_task",
                "enabled": self.weixin_enabled,
                "thread_alive": bool(thread is not None and thread.is_alive()),
            },
            "feishu": {
                "mode": "adapter_runtime",
                "enabled": self.gateway.get_adapter("feishu") is not None,
            },
            "webhook": {
                "mode": "http_route",
                "enabled": self.gateway.get_adapter("webhook") is not None,
            },
        }

    def _run_weixin_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                weixin_runner.run_loop(
                    stop_event=self._stop_event,
                    data_root=self.data_root,
                    gateway=self.gateway,
                )
            except Exception as exc:  # pragma: no cover - defensive service boundary
                logger.exception("Weixin runtime exited unexpectedly: %s", exc)
                self._stop_event.wait(3)
                continue
            if not self._stop_event.is_set():
                time.sleep(3)

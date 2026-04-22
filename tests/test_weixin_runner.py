import runpy
import unittest
from unittest.mock import patch


class _FakeWeixinAdapter:
    def __init__(self) -> None:
        self.account_id = "bot-1"
        self.poll_count = 0

    def assert_configured(self) -> None:
        return None

    def poll_updates(self) -> list[dict]:
        self.poll_count += 1
        raise KeyboardInterrupt


class _FakeGateway:
    def __init__(self, adapter: _FakeWeixinAdapter) -> None:
        self._adapter = adapter

    def get_adapter(self, adapter_name: str):
        if adapter_name == "weixin":
            return self._adapter
        return None


class WeixinRunnerModuleTests(unittest.TestCase):
    def test_module_entrypoint_invokes_main(self) -> None:
        adapter = _FakeWeixinAdapter()
        gateway = _FakeGateway(adapter)
        with patch("im_agent.gateway.build_default_gateway", return_value=gateway) as mocked_build_gateway:
            with patch("im_agent.platforms.weixin.WeixinAdapter", _FakeWeixinAdapter):
                runpy.run_module("im_agent.weixin_runner", run_name="__main__")

        mocked_build_gateway.assert_called_once()
        self.assertEqual(adapter.poll_count, 1)

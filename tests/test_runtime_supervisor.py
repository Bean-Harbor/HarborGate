import threading
import unittest
from unittest.mock import patch

from im_agent.runtime_supervisor import GatewayRuntimeSupervisor


class _FakeGateway:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.adapters = {"webhook": object()}

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def get_adapter(self, adapter_name: str):
        return self.adapters.get(adapter_name)


class RuntimeSupervisorTests(unittest.TestCase):
    def test_supervisor_runs_weixin_runtime_inside_gateway_process(self) -> None:
        gateway = _FakeGateway()
        runtime_started = threading.Event()
        runtime_stopped = threading.Event()

        def fake_run_loop(*, stop_event, data_root, gateway):  # noqa: ANN001
            self.assertEqual(data_root, "data/sessions")
            runtime_started.set()
            stop_event.wait(1)
            runtime_stopped.set()

        supervisor = GatewayRuntimeSupervisor(gateway, data_root="data/sessions", weixin_enabled=True)
        with patch("im_agent.runtime_supervisor.weixin_runner.run_loop", side_effect=fake_run_loop):
            supervisor.start()
            self.assertTrue(runtime_started.wait(1))
            status = supervisor.status()
            self.assertEqual(gateway.started, 1)
            self.assertTrue(status["weixin"]["enabled"])
            self.assertTrue(status["weixin"]["thread_alive"])

            supervisor.stop()

        self.assertTrue(runtime_stopped.is_set())
        self.assertEqual(gateway.stopped, 1)


if __name__ == "__main__":
    unittest.main()

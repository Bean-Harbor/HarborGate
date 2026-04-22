from __future__ import annotations

import json
import logging
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from im_agent.errors import GatewayContractError
from im_agent.gateway import GatewayService, build_default_gateway
from im_agent.platforms.feishu import FeishuAdapter
from im_agent.setup_portal import FileSetupPortalStore, SetupPortalService

logging.basicConfig(
    level=os.getenv("IM_AGENT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


class GatewayRequestHandler(BaseHTTPRequestHandler):
    gateway: GatewayService
    setup_portal: SetupPortalService

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return

        if self.path == "/":
            self._send_json(
                HTTPStatus.OK,
                {
                    "name": "harborgate",
                    "message": (
                        "POST JSON to /messages/webhook to exercise the clean-room gateway. "
                        "Set HARBORBEACON_TASK_API_URL to route turns into HarborBeacon, or use "
                        "harborgate-weixin-login and harborgate-weixin-runner for personal WeChat. "
                        "Open /setup to see Feishu setup, Weixin ingress status, and the redacted gateway snapshot. "
                        "Feishu defaults to long-connection receive mode, so no public webhook is required."
                    ),
                    "setup": self.setup_portal.build_status_payload(request_host=self.headers.get("Host", "")),
                },
            )
            return

        if self.path.startswith("/setup/qr.svg"):
            self._send_svg(
                HTTPStatus.OK,
                self.setup_portal.build_qr_svg(request_host=self.headers.get("Host", "")),
            )
            return

        if self.path.startswith("/setup/qr"):
            self._send_html(
                HTTPStatus.OK,
                self.setup_portal.build_qr_page(request_host=self.headers.get("Host", "")),
            )
            return

        if self.path.startswith("/setup"):
            self._send_html(
                HTTPStatus.OK,
                self.setup_portal.build_setup_page(request_host=self.headers.get("Host", "")),
            )
            return

        if self.path == "/admin/im":
            self._send_html(
                HTTPStatus.OK,
                self.setup_portal.build_setup_page(request_host=self.headers.get("Host", "")),
            )
            return

        if self.path == "/api/setup/status":
            self._send_json(
                HTTPStatus.OK,
                self.setup_portal.build_status_payload(request_host=self.headers.get("Host", "")),
            )
            return

        if self.path == "/api/gateway/status":
            try:
                self._require_service_contract()
                self._require_service_auth()
            except GatewayContractError as exc:
                self._send_json(HTTPStatus(exc.status_code), exc.to_response())
                return
            self._send_json(
                HTTPStatus.OK,
                self.setup_portal.build_gateway_status_payload(
                    request_host=self.headers.get("Host", "")
                ),
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        feishu_adapter = self.gateway.get_adapter("feishu")
        if (
            isinstance(feishu_adapter, FeishuAdapter)
            and feishu_adapter.settings.connection_mode == "webhook"
            and self.path == feishu_adapter.webhook_path
        ):
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                if feishu_adapter.is_url_verification(payload):
                    self._send_json(HTTPStatus.OK, feishu_adapter.build_url_verification_response(payload))
                    return
                self.gateway.handle_inbound("feishu", payload)
            except GatewayContractError as exc:
                self._send_json(HTTPStatus(exc.status_code), exc.to_response())
                return
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except Exception as exc:  # pragma: no cover - defensive server boundary
                logger.exception("Unhandled Feishu webhook error")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        if self.path == "/api/notifications/deliveries":
            payload = self._read_json_body()
            if payload is None:
                return

            try:
                self._require_service_contract()
                self._require_service_auth()
                response = self.gateway.handle_notification_delivery(payload)
            except GatewayContractError as exc:
                self._send_json(HTTPStatus(exc.status_code), exc.to_response())
                return
            except Exception as exc:  # pragma: no cover - defensive server boundary
                logger.exception("Unhandled notification delivery error")
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "error": {
                            "code": "INFRASTRUCTURE_ERROR",
                            "message": str(exc),
                        },
                    },
                )
                return

            self._send_json(HTTPStatus.OK, response)
            return

        if self.path == "/api/setup/feishu/configure":
            payload = self._read_json_body()
            if payload is None:
                return
            status_code, response = self.setup_portal.configure_feishu(
                payload,
                request_host=self.headers.get("Host", ""),
            )
            self._send_json(HTTPStatus(status_code), response)
            return

        match = re.fullmatch(r"/messages/([^/]+)", self.path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        adapter_name = match.group(1)
        payload = self._read_json_body()
        if payload is None:
            return

        try:
            response = self.gateway.handle_inbound(adapter_name, payload)
        except GatewayContractError as exc:
            self._send_json(HTTPStatus(exc.status_code), exc.to_response())
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - defensive server boundary
            logger.exception("Unhandled gateway error")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        self._send_json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), format % args)

    def _read_json_body(self) -> dict | None:
        raw_length = self.headers.get("Content-Length", "0").strip() or "0"
        try:
            content_length = int(raw_length)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return None

        body = self.rfile.read(content_length)
        try:
            return json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be valid JSON"})
            return None

    def _require_service_contract(self) -> None:
        expected = os.getenv("IM_AGENT_CONTRACT_VERSION", "1.5").strip() or "1.5"
        received = self.headers.get("X-Contract-Version", "").strip()
        if received != expected:
            raise GatewayContractError(
                422,
                "CONTRACT_VERSION_MISMATCH",
                f"X-Contract-Version must be {expected}",
            )

    def _require_service_auth(self) -> None:
        expected_token = os.getenv("IM_AGENT_SERVICE_TOKEN", "").strip()
        if not expected_token:
            return
        authorization = self.headers.get("Authorization", "").strip()
        if authorization != f"Bearer {expected_token}":
            raise GatewayContractError(
                401,
                "SERVICE_AUTH_FAILED",
                "Missing or invalid service token",
            )

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, status: HTTPStatus, payload: str) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_svg(self, status: HTTPStatus, payload: str) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def build_handler(gateway: GatewayService, setup_portal: SetupPortalService) -> Callable[..., GatewayRequestHandler]:
    class BoundGatewayRequestHandler(GatewayRequestHandler):
        pass

    BoundGatewayRequestHandler.gateway = gateway
    BoundGatewayRequestHandler.setup_portal = setup_portal
    return BoundGatewayRequestHandler


def main() -> None:
    host = os.getenv("IM_AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("IM_AGENT_PORT", "8787"))
    data_root = os.getenv("IM_AGENT_DATA_DIR", "data/sessions")
    state_root = os.getenv("IM_AGENT_STATE_DIR", str(Path(data_root).parent))
    gateway = build_default_gateway(data_root=data_root)
    setup_portal = SetupPortalService(
        gateway=gateway,
        store=FileSetupPortalStore(state_root),
        bind_host=host,
        bind_port=port,
        public_origin=os.getenv("IM_AGENT_PUBLIC_ORIGIN", ""),
        runtime_root=state_root,
    )
    setup_portal.bootstrap()
    gateway.start()

    server = ThreadingHTTPServer((host, port), build_handler(gateway, setup_portal))
    logger.info("HarborGate listening on http://%s:%s", host, port)
    logger.info("Feishu setup QR page available at http://%s:%s/setup/qr", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down server")
    finally:
        server.server_close()
        gateway.stop()


if __name__ == "__main__":
    main()

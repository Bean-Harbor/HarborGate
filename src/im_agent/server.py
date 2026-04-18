from __future__ import annotations

import json
import logging
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from im_agent.gateway import GatewayService, build_default_gateway

logging.basicConfig(
    level=os.getenv("IM_AGENT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


class GatewayRequestHandler(BaseHTTPRequestHandler):
    gateway: GatewayService

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return

        if self.path == "/":
            self._send_json(
                HTTPStatus.OK,
                {
                    "name": "im-agent-starter",
                    "message": (
                        "POST JSON to /messages/webhook to exercise the clean-room gateway. "
                        "For personal WeChat, use im-agent-weixin-login and im-agent-weixin-runner."
                    ),
                },
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
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

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def build_handler(gateway: GatewayService) -> Callable[..., GatewayRequestHandler]:
    class BoundGatewayRequestHandler(GatewayRequestHandler):
        pass

    BoundGatewayRequestHandler.gateway = gateway
    return BoundGatewayRequestHandler


def main() -> None:
    host = os.getenv("IM_AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("IM_AGENT_PORT", "8787"))
    data_root = os.getenv("IM_AGENT_DATA_DIR", "data/sessions")
    gateway = build_default_gateway(data_root=data_root)

    server = ThreadingHTTPServer((host, port), build_handler(gateway))
    logger.info("IM agent starter listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

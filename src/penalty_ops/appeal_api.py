"""申诉复核子系统的 HTTP JSON 接口,仅依赖标准库。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .appeal_service import AppealService
from .appeal_storage import connect
from .errors import AppealError, ValidationFailed


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到申诉复核服务,便于无网络单元测试。"""

    def __init__(self, service: AppealService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "penalty-appeals"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized) if path != "/users" else None
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/decisions":
                return Response(201, self.service.register_decision(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "decisions":
                return Response(200, self.service.decision(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "decisions" and parts[2] == "ledger":
                return Response(200, {"decision_id": parts[1], "ledger": self.service.decision_ledger(actor, parts[1])})
            if method == "GET" and len(parts) == 3 and parts[0] == "decisions" and parts[2] == "appeals":
                return Response(200, {"decision_id": parts[1], "appeals": self.service.appeals_for_decision(actor, parts[1])})
            if method == "POST" and path == "/authorizations":
                return Response(201, self.service.register_authorization(actor, payload))
            if method == "POST" and path == "/appeals":
                return Response(201, self.service.file_appeal(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "appeals":
                return Response(200, self.service.appeal_detail(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "corrections":
                return Response(201, self.service.request_material_correction(actor, parts[1], payload["required_items"], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "supplements":
                return Response(200, self.service.submit_supplement(actor, parts[1], payload.get("materials")))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "acceptance":
                return Response(200, self.service.review_acceptance(actor, parts[1], bool(payload["accept"]), payload["reason"], payload.get("overdue_reason")))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "decision":
                return Response(200, self.service.decide_reconsideration(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "withdraw":
                return Response(200, self.service.withdraw_appeal(actor, parts[1], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "recusals":
                return Response(201, self.service.recuse_reviewer(actor, parts[1], payload["reason"]))
            if method == "GET" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "recusals":
                return Response(200, {"appeal_id": parts[1], "recusals": self.service.recusals(actor, appeal_id=parts[1])})
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "delivery":
                return Response(201, self.service.record_delivery(actor, parts[1], payload["method"], payload["recipient"]))
            if method == "GET" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "delivery":
                return Response(200, self.service.delivery(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "evidence-citations":
                return Response(200, {"appeal_id": parts[1], "citations": self.service.review_evidence(actor, parts[1])})
            if method == "GET" and path == "/overdue":
                return Response(200, self.service.overdue_report(actor))
            if method == "GET" and path == "/dunning-queue":
                return Response(200, self.service.dunning_queue(actor))
            if method == "GET" and len(parts) == 3 and parts[0] == "audit":
                return Response(200, {"events": self.service.audit_events(actor, parts[1], parts[2])})
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except AppealError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication, lock: threading.Lock | None = None):
    # 单个 SQLite 连接跨线程共享,请求在服务器边界串行化
    request_lock = lock or threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "PenaltyAppeals/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with request_lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动处罚申诉复核服务")
    parser.add_argument("--database", type=Path, default=Path("appeals.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(AppealService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

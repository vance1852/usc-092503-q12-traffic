"""无第三方依赖的处罚申诉 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import PenaltyAppealError, ValidationFailed
from .service import PenaltyAppealService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """把 HTTP 路由映射到申诉领域服务，便于无网络单元测试。"""

    def __init__(self, service: PenaltyAppealService) -> None:
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

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        service = self.service
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "penalty-appeal"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)

            if method == "POST" and path == "/users":
                return Response(201, service.create_user(
                    actor, payload["user_id"], payload["display_name"], payload["role"]
                ))
            if method == "POST" and path == "/parties":
                return Response(201, service.register_party(
                    actor, payload["party_id"], payload["party_type"], payload["name"],
                    payload.get("id_number"),
                ))
            if method == "POST" and path == "/authorizations":
                return Response(201, service.grant_authorization(actor, payload))
            if method == "POST" and path == "/penalty_decisions":
                return Response(201, service.register_decision(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "penalty_decisions":
                return Response(200, service.read_decision(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "penalty_decisions" and parts[2] == "enforcement_actions":
                return Response(200, {"actions": service.list_enforcement_actions(actor, parts[1])})
            if method == "POST" and path == "/suspension_rules":
                return Response(200, service.set_suspension_rule(
                    actor, payload["action_type"], bool(payload["suspend_on_acceptance"]), payload["note"]
                ))

            if method == "POST" and path == "/appeals":
                return Response(201, service.register_appeal(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "appeals":
                return Response(200, service.read_appeal(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "history":
                return Response(200, service.appeal_history(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "corrections":
                return Response(201, service.issue_correction_notice(
                    actor, parts[1], payload["required_items"],
                    int(payload.get("cure_days", 7)), payload.get("note", "")
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "cure":
                return Response(200, service.cure_resubmit(actor, parts[1], payload["materials"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "deem_withdrawn":
                return Response(200, service.close_unremedied(actor, parts[1], payload["note"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "acceptance":
                return Response(200, service.review_acceptance(
                    actor, parts[1], bool(payload["accept"]), payload["note"]
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "reviewers":
                return Response(201, service.assign_reviewer(actor, parts[1], payload["reviewer_id"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "recusals":
                return Response(201, service.request_recusal(
                    actor, parts[1], payload["reviewer_id"], payload["reason"], payload["requested_by"]
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "self_recuse":
                return Response(201, service.self_recuse(actor, parts[1], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "review_decision":
                return Response(200, service.decide_review(
                    actor, parts[1], payload["conclusion"], payload["reason_text"],
                    payload.get("adjustments"),
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "withdraw":
                return Response(200, service.withdraw_appeal(
                    actor, parts[1], payload["requesting_party_id"], payload["reason"]
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "services":
                return Response(201, service.record_service(
                    actor, parts[1], payload["stage"], payload["method"],
                    payload["recipient_party_id"], payload["document_sha256"]
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "appeals" and parts[2] == "overdue_explanation":
                return Response(201, service.explain_overdue(
                    actor, parts[1], payload["stage"], payload["reason_code"], payload["reason_detail"]
                ))
            if method == "POST" and path == "/overdue/detect":
                return Response(200, service.detect_overdue(actor))
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except PenaltyAppealError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PenaltyAppeal/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
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
    parser = argparse.ArgumentParser(description="启动处罚申诉与执行联动服务")
    parser.add_argument("--database", type=Path, default=Path("penalty_appeal.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    service = PenaltyAppealService(connection)
    service.bootstrap()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(service)))
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

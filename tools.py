"""Hermes tool handlers for Tern delegations."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .client import DEFAULT_BASE_URL, RunnerClient, RunnerProtocolError
from .routing import RouteNotFoundError, RoutingError
from .runtime import RunnerSession


def build_session(
    endpoint: Callable[[], str],
    route_guard: Callable[[str, str], Any],
) -> RunnerSession:
    def client_factory() -> RunnerClient:
        credential = os.environ.get("TERN_RUNNER_CREDENTIAL", "").strip()
        if not credential:
            raise RunnerProtocolError(
                "Hermes is not connected to Tern. Run `hermes tern connect`.",
                code="not_connected",
            )
        return RunnerClient(endpoint() or DEFAULT_BASE_URL, credential)

    return RunnerSession(client_factory, route_guard=route_guard)


def _json_result(action: Callable[[], dict[str, Any]]) -> str:
    try:
        return json.dumps(action(), separators=(",", ":"), ensure_ascii=False)
    except RunnerProtocolError as error:
        return json.dumps(
            {
                "error": str(error),
                "code": error.code,
                "retryable": error.retryable,
                "status": error.status,
            },
            separators=(",", ":"),
        )
    except RouteNotFoundError as error:
        return json.dumps(
            {"error": str(error), "code": "route_not_configured"},
            separators=(",", ":"),
        )
    except RoutingError as error:
        return json.dumps(
            {"error": str(error), "code": "invalid_route"},
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        return json.dumps({"error": str(error), "code": "invalid_input"}, separators=(",", ":"))
    except Exception as error:  # Hermes handlers must never raise into the tool loop.
        return json.dumps(
            {"error": f"Tern plugin failed safely: {type(error).__name__}", "code": "plugin_error"},
            separators=(",", ":"),
        )


def handlers(session: RunnerSession) -> dict[str, Callable[..., str]]:
    def claim_next(args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        return _json_result(
            lambda: session.claim_next(
                str(args.get("organization_id", "")),
                str(args.get("project_id", "")),
            )
        )

    def progress(args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        return _json_result(
            lambda: session.progress(
                str(args.get("message", "")),
                phase=args.get("phase"),
                percent=args.get("percent"),
                known_gap=args.get("known_gap"),
            )
        )

    def finish(args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        return _json_result(
            lambda: session.finish(
                outcome=str(args.get("outcome", "")),
                summary=str(args.get("summary", "")),
                work_performed=str(args.get("work_performed", "")),
                known_gaps=args.get("known_gaps"),
                artifacts=args.get("artifacts"),
            )
        )

    def status(args: dict[str, Any], **kwargs: Any) -> str:
        del args, kwargs
        return _json_result(session.status)

    return {
        "tern_claim_next": claim_next,
        "tern_progress": progress,
        "tern_finish": finish,
        "tern_run_status": status,
    }

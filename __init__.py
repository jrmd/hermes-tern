"""Tern for Hermes Agent."""

from pathlib import Path

from . import schemas
from .cli import make_handler, setup_parser
from .routing import ProjectRouter, RoutingConfig, RoutingError
from .tools import build_session, handlers


def register(ctx):
    endpoint = lambda: ctx.get_config(
        "endpoint", default="https://gettern.app/api/runner/v1"
    )
    def route_guard(organization_id: str, project_id: str):
        raw = ctx.get_config("routing", default={"version": 1, "routes": []})
        config = RoutingConfig.from_dict(raw)
        route = ProjectRouter(config).require(organization_id, project_id)
        current = Path.cwd().resolve(strict=True)
        if current != route.checkout_path:
            raise RoutingError(
                "Hermes is running in a different checkout than the configured Tern project route"
            )
        return route

    session = build_session(endpoint, route_guard)
    registered = handlers(session)
    ctx.register_tool(
        name="tern_claim_next",
        toolset="tern",
        schema=schemas.TERN_CLAIM_NEXT,
        handler=registered["tern_claim_next"],
    )
    ctx.register_tool(
        name="tern_progress",
        toolset="tern",
        schema=schemas.TERN_PROGRESS,
        handler=registered["tern_progress"],
    )
    ctx.register_tool(
        name="tern_finish",
        toolset="tern",
        schema=schemas.TERN_FINISH,
        handler=registered["tern_finish"],
    )
    ctx.register_tool(
        name="tern_run_status",
        toolset="tern",
        schema=schemas.TERN_RUN_STATUS,
        handler=registered["tern_run_status"],
    )
    ctx.register_tool(
        name="tern_update_issue_status",
        toolset="tern",
        schema=schemas.TERN_UPDATE_ISSUE_STATUS,
        handler=registered["tern_update_issue_status"],
    )
    ctx.register_cli_command(
        name="tern",
        help="Connect Hermes to Tern and manage delegated runs",
        description="Tern agent runner integration",
        setup_fn=setup_parser,
        handler_fn=make_handler(ctx),
    )
    skill = Path(__file__).parent / "skills" / "delegated-run" / "SKILL.md"
    ctx.register_skill("delegated-run", skill)

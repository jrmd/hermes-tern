"""`hermes tern` connection, routing, and scheduler commands."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .client import DEFAULT_BASE_URL, RunnerClient, RunnerProtocolError, normalize_base_url
from .monitor import render_monitor_script
from .routing import ProjectRoute, RoutingConfig


LEGACY_JOB_NAME = "Tern delegated agent"
JOB_PREFIX = "Tern delegated agent ["
CRON_SKILL_NAME = "tern-runner-delegated-run-v1"
MONITOR_PREFIX = "tern_route_monitor_"
PLUGIN_DIR = Path(__file__).resolve().parent
MONITOR_SOURCE = PLUGIN_DIR / "monitor.py"
SKILL_SOURCE = PLUGIN_DIR / "skills" / "delegated-run" / "SKILL.md"


def _credential() -> str:
    return os.environ.get("TERN_RUNNER_CREDENTIAL", "").strip()


def _route_token(route: ProjectRoute) -> str:
    value = f"{route.organization_id}\0{route.project_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


def _job_name(route: ProjectRoute) -> str:
    return f"{JOB_PREFIX}{_route_token(route)}]"


def _monitor_name(route: ProjectRoute) -> str:
    return f"{MONITOR_PREFIX}{_route_token(route)}.py"


def _routing(ctx: Any) -> RoutingConfig:
    raw = ctx.get_config("routing", default={"version": 1, "routes": []})
    return RoutingConfig.from_dict(raw)


def _save_routing(ctx: Any, routes: tuple[ProjectRoute, ...]) -> RoutingConfig:
    config = RoutingConfig(routes=routes)
    ctx.set_config("routing", config.to_dict())
    return config


def _jobs() -> list[dict[str, Any]]:
    from cron.jobs import list_jobs

    return [
        job
        for job in list_jobs(include_disabled=True)
        if job.get("name") == LEGACY_JOB_NAME
        or str(job.get("name", "")).startswith(JOB_PREFIX)
    ]


def _gateway_running() -> bool | None:
    try:
        from gateway.status import is_gateway_runtime_lock_active

        return bool(is_gateway_runtime_lock_active())
    except Exception:
        return None


def _install_route_monitor(route: ProjectRoute) -> Path:
    from hermes_constants import get_hermes_home

    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shared_monitor = scripts_dir / "tern_ready_monitor.py"
    shutil.copy2(MONITOR_SOURCE, shared_monitor)
    shared_monitor.chmod(0o700)
    destination = scripts_dir / _monitor_name(route)
    destination.write_text(
        render_monitor_script(
            route.organization_id,
            route.project_id,
        ),
        encoding="utf-8",
    )
    destination.chmod(0o700)
    return destination


def _install_cron_skill() -> Path:
    """Install a collision-safe scheduler copy for Hermes 0.20.x."""
    from hermes_constants import get_hermes_home

    destination = get_hermes_home() / "skills" / CRON_SKILL_NAME / "SKILL.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = SKILL_SOURCE.read_bytes()
    if destination.exists() and destination.read_bytes() != source:
        raise RuntimeError(
            f"Hermes skill path already contains unrelated content: {destination}"
        )
    destination.write_bytes(source)
    destination.chmod(0o644)
    return destination


def _job_prompt(route: ProjectRoute) -> str:
    return (
        "Process one ready Tern delegation for the exact route "
        f"organization_id={route.organization_id!r}, project_id={route.project_id!r}. "
        "Follow the attached delegated-run skill. Call tern_claim_next exactly once "
        "with those two IDs, obey the returned authority, report meaningful progress, "
        "and finish exactly once."
    )


def _job_by_name(name: str) -> dict[str, Any] | None:
    return next((job for job in _jobs() if job.get("name") == name), None)


def _upsert_job(
    route: ProjectRoute,
    *,
    schedule: str,
    model: str | None,
    provider: str | None,
    reasoning_effort: str | None,
) -> tuple[str, str]:
    from cron.jobs import resume_job
    from tools.cronjob_tools import cronjob

    monitor = _install_route_monitor(route)
    name = _job_name(route)
    existing = _job_by_name(name)
    common = {
        "schedule": schedule,
        "prompt": _job_prompt(route),
        "skills": [CRON_SKILL_NAME],
        "workdir": os.fspath(route.checkout_path),
        "monitor_script": monitor.name,
        "model": model,
        "provider": provider,
        "reasoning_effort": reasoning_effort,
    }
    if existing:
        payload = json.loads(
            cronjob(action="update", job_id=existing["id"], name=name, **common)
        )
        if not payload.get("success"):
            raise RuntimeError(payload.get("error", f"Hermes could not update {name}"))
        if not existing.get("enabled", True):
            resume_job(existing["id"])
        return existing["id"], "updated"

    payload = json.loads(
        cronjob(
            action="create",
            name=name,
            repeat=0,
            deliver="local",
            **common,
        )
    )
    if not payload.get("success"):
        raise RuntimeError(payload.get("error", f"Hermes could not create {name}"))
    return payload["job_id"], "created"


def _pause_stale_jobs(routes: tuple[ProjectRoute, ...]) -> int:
    from cron.jobs import pause_job

    desired = {_job_name(route) for route in routes}
    paused = 0
    for job in _jobs():
        if job.get("name") in desired or not job.get("enabled", True):
            continue
        pause_job(job["id"], reason="Tern route is not configured on this Hermes install")
        paused += 1
    return paused


def _scheduler_settings(
    ctx: Any,
    *,
    schedule: str | None,
    model: str | None,
    provider: str | None,
    reasoning_effort: str | None,
) -> dict[str, str | None]:
    previous = ctx.get_config("scheduler", default={})
    if not isinstance(previous, dict):
        previous = {}
    settings: dict[str, str | None] = {
        "schedule": schedule or previous.get("schedule") or "every 1m",
        "model": model if model is not None else previous.get("model"),
        "provider": provider if provider is not None else previous.get("provider"),
        "reasoningEffort": (
            reasoning_effort
            if reasoning_effort is not None
            else previous.get("reasoningEffort")
        ),
    }
    ctx.set_config("scheduler", settings)
    return settings


def _start_all(
    ctx: Any,
    *,
    schedule: str | None,
    model: str | None,
    provider: str | None,
    reasoning_effort: str | None,
) -> tuple[int, int]:
    if not _credential():
        raise RuntimeError("Hermes is not connected to Tern; run `hermes tern connect`")
    routes = _routing(ctx).routes
    if not routes:
        raise RuntimeError(
            "No project routes are configured; run `hermes tern projects add` first"
        )
    settings = _scheduler_settings(
        ctx,
        schedule=schedule,
        model=model,
        provider=provider,
        reasoning_effort=reasoning_effort,
    )
    _install_cron_skill()
    changed = 0
    for route in routes:
        _upsert_job(
            route,
            schedule=str(settings["schedule"]),
            model=settings["model"],
            provider=settings["provider"],
            reasoning_effort=settings["reasoningEffort"],
        )
        changed += 1
    return changed, _pause_stale_jobs(routes)


def _restart_gateway() -> bool:
    executable = shutil.which("hermes")
    if not executable:
        return False
    completed = subprocess.run(
        [executable, "gateway", "restart"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    return completed.returncode == 0


def _add_scheduler_arguments(parser: Any) -> None:
    parser.add_argument("--schedule", default=None, help="Polling schedule (default: every 1m)")
    parser.add_argument("--model", help="Optional Hermes model override")
    parser.add_argument("--provider", help="Provider paired with --model")
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    )


def setup_parser(parser: Any) -> None:
    subparsers = parser.add_subparsers(dest="tern_action", required=True)

    connect = subparsers.add_parser("connect", help="Pair Hermes with a Tern runner")
    connect.add_argument("--url", default=DEFAULT_BASE_URL, help="Tern runner protocol URL")
    connect.add_argument("--no-start", action="store_true", help="Save without starting routes")
    connect.add_argument("--no-restart", action="store_true", help="Do not restart the gateway")
    _add_scheduler_arguments(connect)

    projects = subparsers.add_parser("projects", help="Manage Tern project checkout routes")
    project_actions = projects.add_subparsers(dest="tern_project_action", required=True)
    add = project_actions.add_parser("add", help="Map one exact Tern project to a checkout")
    add.add_argument("--organization-id", required=True, help="Tern organization ID")
    add.add_argument("--project-id", required=True, help="Tern project ID")
    add.add_argument("--path", required=True, help="Absolute Git repository root")
    add.add_argument("--label", help="Human-readable local label")
    add.add_argument("--no-start", action="store_true", help="Save without reconciling jobs")
    remove = project_actions.add_parser("remove", help="Remove one exact checkout route")
    remove.add_argument("--organization-id", required=True)
    remove.add_argument("--project-id", required=True)
    project_actions.add_parser("list", help="List configured checkout routes")

    start = subparsers.add_parser("start", help="Start pickup for every configured project")
    _add_scheduler_arguments(start)
    subparsers.add_parser("stop", help="Pause every Tern pickup job")
    status = subparsers.add_parser("status", help="Show connection, routes, and jobs")
    status.add_argument("--live", action="store_true", help="Verify every route against Tern")
    subparsers.add_parser("disconnect", help="Remove the credential and Tern scheduler assets")


def _connect(ctx: Any, args: Any) -> int:
    from hermes_cli.config import save_env_value
    from hermes_cli.secret_prompt import masked_secret_prompt

    endpoint = normalize_base_url(args.url)
    credential = _credential()
    if not credential:
        try:
            credential = masked_secret_prompt(
                "Paste the one-time Tern runner credential (hidden): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            credential = ""
    if not credential:
        print("Tern connection cancelled; no credential was saved.")
        return 2
    try:
        RunnerClient(endpoint, credential).list_ready_runs(limit=1)
    except (RunnerProtocolError, ValueError) as error:
        print(f"Tern rejected the connection: {error}")
        return 2

    save_env_value("TERN_RUNNER_CREDENTIAL", credential)
    save_env_value("TERN_RUNNER_URL", endpoint)
    ctx.set_config("endpoint", endpoint)
    os.environ["TERN_RUNNER_CREDENTIAL"] = credential
    os.environ["TERN_RUNNER_URL"] = endpoint
    routes = _routing(ctx).routes
    print(f"Connected to Tern. Configured project routes: {len(routes)}.")
    changed = 0
    if routes and not args.no_start:
        changed, paused = _start_all(
            ctx,
            schedule=args.schedule,
            model=args.model,
            provider=args.provider,
            reasoning_effort=args.reasoning_effort,
        )
        print(f"Reconciled {changed} project job(s); paused {paused} stale job(s).")
    elif not routes:
        print("Add each local checkout with `hermes tern projects add`, then run `hermes tern start`.")
    if not args.no_restart and (changed or _gateway_running()):
        print(
            "Hermes gateway restarted with the Tern connection loaded."
            if _restart_gateway()
            else "Connection saved, but the Hermes gateway could not be restarted automatically."
        )
    return 0


def _projects_add(ctx: Any, args: Any) -> int:
    route = ProjectRoute(
        organization_id=args.organization_id,
        project_id=args.project_id,
        checkout_path=args.path,
        label=args.label,
    )
    existing = _routing(ctx).routes
    routes = tuple(candidate for candidate in existing if candidate.key != route.key) + (route,)
    _save_routing(ctx, routes)
    print(
        f"Mapped {route.organization_id}/{route.project_id} to {route.checkout_path}"
        + (f" ({route.label})" if route.label else "")
        + "."
    )
    if _credential() and not args.no_start:
        settings = _scheduler_settings(
            ctx, schedule=None, model=None, provider=None, reasoning_effort=None
        )
        _install_cron_skill()
        job_id, action = _upsert_job(
            route,
            schedule=str(settings["schedule"]),
            model=settings["model"],
            provider=settings["provider"],
            reasoning_effort=settings["reasoningEffort"],
        )
        print(f"Project pickup {action} ({job_id}).")
    return 0


def _projects_remove(ctx: Any, args: Any) -> int:
    config = _routing(ctx)
    key = (args.organization_id, args.project_id)
    route = next((candidate for candidate in config.routes if candidate.key == key), None)
    if route is None:
        print(f"No route is configured for {key[0]}/{key[1]}.")
        return 0
    _save_routing(ctx, tuple(candidate for candidate in config.routes if candidate.key != key))
    job = _job_by_name(_job_name(route))
    if job and job.get("enabled", True):
        from cron.jobs import pause_job

        pause_job(job["id"], reason="Tern project route removed")
    print(f"Removed route {key[0]}/{key[1]}; its scheduler job is paused.")
    return 0


def _projects_list(ctx: Any) -> int:
    routes = _routing(ctx).routes
    if not routes:
        print("No Tern project routes are configured.")
        return 0
    jobs = {job.get("name"): job for job in _jobs()}
    for route in routes:
        job = jobs.get(_job_name(route))
        state = "active" if job and job.get("enabled", True) else "paused/not installed"
        label = f" [{route.label}]" if route.label else ""
        print(
            f"{route.organization_id}/{route.project_id}{label}\n"
            f"  checkout: {route.checkout_path}\n"
            f"  pickup:   {state}"
        )
    return 0


def _start(ctx: Any, args: Any) -> int:
    changed, paused = _start_all(
        ctx,
        schedule=args.schedule,
        model=args.model,
        provider=args.provider,
        reasoning_effort=args.reasoning_effort,
    )
    print(f"Started or updated {changed} project job(s); paused {paused} stale job(s).")
    return 0


def _stop() -> int:
    from cron.jobs import pause_job

    active = [job for job in _jobs() if job.get("enabled", True)]
    for job in active:
        pause_job(job["id"], reason="paused by hermes tern stop")
    print(f"Paused {len(active)} Tern pickup job(s).")
    return 0


def _status(ctx: Any, live: bool) -> int:
    connected = bool(_credential())
    gateway = _gateway_running()
    routes = _routing(ctx).routes
    jobs = {job.get("name"): job for job in _jobs()}
    print(f"Connection: {'configured' if connected else 'not configured'}")
    print(f"Endpoint: {ctx.get_config('endpoint', default=DEFAULT_BASE_URL)}")
    print(f"Gateway: {('running' if gateway else 'stopped') if gateway is not None else 'unknown'}")
    print(f"Project routes: {len(routes)}")
    failures = 0
    client = (
        RunnerClient(ctx.get_config("endpoint", default=DEFAULT_BASE_URL), _credential())
        if live and connected
        else None
    )
    for route in routes:
        job = jobs.get(_job_name(route))
        state = "active" if job and job.get("enabled", True) else "paused/not installed"
        detail = ""
        if client:
            try:
                runs = client.list_ready_runs(
                    limit=100,
                    organization_id=route.organization_id,
                    project_id=route.project_id,
                )["runs"]
                detail = f"; authenticated, {len(runs)} ready"
            except (RunnerProtocolError, ValueError) as error:
                failures += 1
                detail = f"; live check failed: {error}"
        print(
            f"- {route.label or route.project_id}: {route.checkout_path} ({state}{detail})"
        )
    legacy = jobs.get(LEGACY_JOB_NAME)
    if legacy:
        print(
            "Legacy unscoped job: "
            + ("active (unsafe; run `hermes tern start`)" if legacy.get("enabled", True) else "paused")
        )
    return 2 if failures else 0


def _disconnect(ctx: Any) -> int:
    from cron.jobs import remove_job
    from hermes_cli.config import save_env_value
    from hermes_constants import get_hermes_home

    for job in _jobs():
        remove_job(job["id"])
    scripts = get_hermes_home() / "scripts"
    if scripts.is_dir():
        for path in scripts.glob(f"{MONITOR_PREFIX}*.py"):
            if path.is_file():
                path.unlink()
        shared_monitor = scripts / "tern_ready_monitor.py"
        if shared_monitor.is_file() and shared_monitor.read_bytes() == MONITOR_SOURCE.read_bytes():
            shared_monitor.unlink()
    skill = get_hermes_home() / "skills" / CRON_SKILL_NAME / "SKILL.md"
    if skill.is_file() and skill.read_bytes() == SKILL_SOURCE.read_bytes():
        skill.unlink()
        try:
            skill.parent.rmdir()
        except OSError:
            pass
    save_env_value("TERN_RUNNER_CREDENTIAL", "")
    save_env_value("TERN_RUNNER_URL", "")
    os.environ.pop("TERN_RUNNER_CREDENTIAL", None)
    os.environ.pop("TERN_RUNNER_URL", None)
    ctx.set_config("endpoint", DEFAULT_BASE_URL)
    print("Disconnected Tern and removed its scheduler jobs and generated assets.")
    return 0


def make_handler(ctx: Any):
    def handle(args: Any) -> int:
        try:
            if args.tern_action == "connect":
                return _connect(ctx, args)
            if args.tern_action == "projects":
                if args.tern_project_action == "add":
                    return _projects_add(ctx, args)
                if args.tern_project_action == "remove":
                    return _projects_remove(ctx, args)
                return _projects_list(ctx)
            if args.tern_action == "start":
                return _start(ctx, args)
            if args.tern_action == "stop":
                return _stop()
            if args.tern_action == "status":
                return _status(ctx, args.live)
            if args.tern_action == "disconnect":
                return _disconnect(ctx)
        except (LookupError, OSError, PermissionError, RuntimeError, ValueError) as error:
            print(f"Tern command failed: {error}")
            return 2
        print("Unknown Tern command")
        return 2

    return handle

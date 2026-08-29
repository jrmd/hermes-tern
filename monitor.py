#!/usr/bin/env python3
"""Cheap, route-bound monitor used by Hermes cron before waking an agent.

This file is copied into Hermes' scripts directory and therefore deliberately
has no imports from the plugin package.  The route is non-secret configuration
(`organizationId` + `projectId`); the runner credential is read only from the
Hermes-managed environment at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_BASE_URL = "https://gettern.app/api/runner/v1"
USER_AGENT = "hermes-tern-plugin/0.2.4"
MAX_RESPONSE_BYTES = 64_000
MAX_ROUTE_CONFIG_BYTES = 2_048
MAX_RUNS_PER_POLL = 100

ORGANIZATION_ID_ENV = "TERN_RUNNER_ORGANIZATION_ID"
PROJECT_ID_ENV = "TERN_RUNNER_PROJECT_ID"
ROUTE_CONFIG_ENV = "TERN_RUNNER_ROUTE"

# Short aliases make the rendered route easy to use with generic dotenv
# tooling, while the runner-prefixed names remain canonical.  Conflicting
# aliases are rejected instead of silently selecting the wrong tenant.
_ORGANIZATION_ID_ALIASES = (ORGANIZATION_ID_ENV, "TERN_ORGANIZATION_ID")
_PROJECT_ID_ALIASES = (PROJECT_ID_ENV, "TERN_PROJECT_ID")


class RouteConfigurationError(ValueError):
    """Raised when a monitor route is absent, malformed, or ambiguous."""


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RouteConfigurationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise RouteConfigurationError(f"{field} is required")
    if len(normalized) > 256:
        raise RouteConfigurationError(f"{field} is too long")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized):
        raise RouteConfigurationError(f"{field} contains control characters")
    return normalized


@dataclass(frozen=True)
class RouteBinding:
    """The exact Tern tenant/project route a monitor is allowed to observe."""

    organization_id: str
    project_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "organization_id",
            _identifier(self.organization_id, "organizationId"),
        )
        object.__setattr__(self, "project_id", _identifier(self.project_id, "projectId"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RouteBinding":
        unknown = set(value) - {"organizationId", "projectId", "organization_id", "project_id"}
        if unknown:
            raise RouteConfigurationError("route configuration contains unknown fields")
        organization_id = value.get("organizationId", value.get("organization_id"))
        project_id = value.get("projectId", value.get("project_id"))
        return cls(organization_id, project_id)

    def matches(self, run: Mapping[str, Any]) -> bool:
        return (
            run.get("organizationId") == self.organization_id
            and run.get("projectId") == self.project_id
        )

    def as_environment(self) -> dict[str, str]:
        """Return non-secret environment values suitable for an installer."""
        return {
            ORGANIZATION_ID_ENV: self.organization_id,
            PROJECT_ID_ENV: self.project_id,
        }

    def as_mapping(self) -> dict[str, str]:
        return {"organizationId": self.organization_id, "projectId": self.project_id}


def bind_route(organization_id: str, project_id: str) -> RouteBinding:
    """Create the explicit route used by an installer or an embedding caller."""
    return RouteBinding(organization_id, project_id)


def _aliased_environment_value(
    environment: Mapping[str, str], aliases: Sequence[str], field: str
) -> str | None:
    values: set[str] = set()
    for alias in aliases:
        raw = environment.get(alias)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise RouteConfigurationError(f"{field} must be a string")
        normalized = raw.strip()
        if normalized:
            values.add(normalized)
    if len(values) > 1:
        raise RouteConfigurationError(f"conflicting {field} configuration")
    return next(iter(values), None)


def route_from_environment(environment: Mapping[str, str] | None = None) -> RouteBinding | None:
    """Load a route from dotenv-style values without ever reading a secret.

    ``TERN_RUNNER_ROUTE`` is a compact JSON form useful to renderers.  The two
    explicit ID variables are the preferred install form.  If no route values
    are present, ``None`` is returned so callers can report a safe
    ``not_configured`` state instead of accidentally listing every tenant's
    work.
    """

    values = os.environ if environment is None else environment
    encoded = values.get(ROUTE_CONFIG_ENV)
    route_from_blob: RouteBinding | None = None
    if encoded is not None and not isinstance(encoded, str):
        raise RouteConfigurationError("route configuration must be a string")
    if encoded is not None and encoded.strip():
        if len(encoded.encode("utf-8")) > MAX_ROUTE_CONFIG_BYTES:
            raise RouteConfigurationError("route configuration is too large")
        try:
            decoded = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RouteConfigurationError("route configuration is invalid JSON") from error
        if not isinstance(decoded, Mapping):
            raise RouteConfigurationError("route configuration must be an object")
        route_from_blob = RouteBinding.from_mapping(decoded)

    organization_id = _aliased_environment_value(
        values, _ORGANIZATION_ID_ALIASES, "organizationId"
    )
    project_id = _aliased_environment_value(values, _PROJECT_ID_ALIASES, "projectId")
    if (organization_id is None) != (project_id is None):
        raise RouteConfigurationError("organizationId and projectId must be configured together")

    route_from_ids = (
        RouteBinding(organization_id, project_id)
        if organization_id is not None and project_id is not None
        else None
    )
    if (
        route_from_blob is not None
        and route_from_ids is not None
        and route_from_blob != route_from_ids
    ):
        raise RouteConfigurationError("route configuration disagrees with explicit IDs")
    return route_from_blob or route_from_ids


def render_route_config(organization_id: str, project_id: str) -> str:
    """Render a deterministic, secret-free JSON route for installation."""
    route = bind_route(organization_id, project_id)
    return json.dumps(route.as_mapping(), sort_keys=True, separators=(",", ":"))


def render_route_environment(organization_id: str, project_id: str) -> str:
    """Render dotenv lines containing only the route, never a credential."""
    route = bind_route(organization_id, project_id)
    return "\n".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}"
        for name, value in route.as_environment().items()
    )


def render_monitor_script(
    organization_id: str,
    project_id: str,
    *,
    monitor_module: str = "tern_ready_monitor",
) -> str:
    """Render a route-bound wrapper that can be installed beside this script.

    Hermes cron accepts a script name, not per-job environment values.  An
    installer can save this secret-free wrapper under a unique script name;
    the wrapper sets only ``TERN_RUNNER_ROUTE`` and delegates to the installed
    monitor.  The credential remains inherited at runtime from Hermes.
    """
    if not monitor_module.isidentifier():
        raise ValueError("monitor_module must be a Python module name")
    route_blob = render_route_config(organization_id, project_id)
    return (
        "#!/usr/bin/env python3\n"
        '"""Installed Tern monitor wrapper; contains no runner credential."""\n'
        "import os\n\n"
        f"os.environ[{ROUTE_CONFIG_ENV!r}] = {route_blob!r}\n"
        f"from {monitor_module} import main\n\n"
        "main()\n"
    )


def _run_id(run: Mapping[str, Any]) -> str | None:
    candidate = run.get("runId") or run.get("id")
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    return candidate.strip()


def _run_version(run: Mapping[str, Any]) -> int | None:
    candidate = run.get("version")
    if isinstance(candidate, bool):
        return None
    if isinstance(candidate, int):
        return candidate if 0 < candidate <= 2_147_483_647 else None
    # Be tolerant of a renderer/fixture serializing this integer as text, but
    # normalize it before sorting and emission so equivalent payloads are stable.
    if isinstance(candidate, str) and candidate.strip().isdigit():
        try:
            normalized = int(candidate.strip())
        except ValueError:
            return None
        return normalized if 0 < normalized <= 2_147_483_647 else None
    return None


def select_ready_runs(
    runs: Sequence[Any], route: RouteBinding, *, max_runs: int = MAX_RUNS_PER_POLL
) -> list[dict[str, Any]]:
    """Project only this route's minimal identifiers in deterministic order."""
    if not isinstance(route, RouteBinding):
        raise TypeError("route must be a RouteBinding")
    bounded_max = max(0, min(int(max_runs), MAX_RUNS_PER_POLL))
    selected: set[tuple[str, int, str]] = set()
    for candidate in runs:
        if not isinstance(candidate, Mapping) or not route.matches(candidate):
            continue
        run_id = _run_id(candidate)
        version = _run_version(candidate)
        profile_id = candidate.get("profileId")
        if (
            run_id is None
            or version is None
            or not isinstance(profile_id, str)
            or not profile_id.strip()
        ):
            continue
        selected.add((run_id, version, profile_id.strip()))

    ordered = sorted(selected, key=lambda item: (item[0], item[1], item[2]))
    return [
        {"runId": run_id, "version": version, "profileId": profile_id}
        for run_id, version, profile_id in ordered[:bounded_max]
    ]


def _payload_runs(payload: Any) -> list[Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("response must be an object")
    data = payload.get("data", payload)
    if not isinstance(data, Mapping) or not isinstance(data.get("runs"), list):
        raise ValueError("response did not contain runs")
    return data["runs"]


def build_stable_output(payload: Any, route: RouteBinding) -> dict[str, Any]:
    """Build the sole model wake-up signal from an already-decoded response."""
    try:
        runs = _payload_runs(payload)
    except ValueError:
        return {"state": "invalid_response"}
    return {"state": "ready", "runs": select_ready_runs(runs, route)}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable(value: Any, stream: TextIO | None = None) -> None:
    print(stable_json(value), file=stream)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


def endpoint_is_safe(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url)
        hostname = parsed.hostname
    except ValueError:
        return False
    if not hostname or parsed.username or parsed.password or parsed.fragment:
        return False
    loopback = hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    return parsed.scheme == "https" or (parsed.scheme == "http" and loopback)


def main(route: RouteBinding | None = None) -> None:
    credential = os.environ.get("TERN_RUNNER_CREDENTIAL", "").strip()
    base_url = os.environ.get("TERN_RUNNER_URL", DEFAULT_BASE_URL).rstrip("/")
    if not credential:
        stable({"state": "not_connected"})
        return
    if not endpoint_is_safe(base_url):
        stable({"state": "invalid_endpoint"})
        return
    if route is None:
        try:
            route = route_from_environment()
        except RouteConfigurationError:
            stable({"state": "invalid_route"})
            return
    if route is None:
        # Never make an unscoped list request.  A missing route is a setup
        # problem, not permission to observe every project on the credential.
        stable({"state": "not_configured"})
        return

    request = Request(
        f"{base_url}/runs?{urlencode({'limit': MAX_RUNS_PER_POLL, 'organizationId': route.organization_id, 'projectId': route.project_id})}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {credential}",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        response = build_opener(NoRedirect()).open(request, timeout=10)
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            stable({"state": "response_too_large"})
            return
        payload = json.loads(body.decode("utf-8"))
        stable(build_stable_output(payload, route))
    except HTTPError as error:
        stable({"state": "http_error", "status": error.code})
    except (URLError, TimeoutError, OSError):
        stable({"state": "unreachable"})
    except (UnicodeDecodeError, json.JSONDecodeError):
        stable({"state": "invalid_response"})


if __name__ == "__main__":
    main()

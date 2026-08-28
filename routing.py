"""Safe project-to-checkout routing for delegated Hermes runs.

Routing is deliberately a small, dependency-free boundary.  A route is keyed
by the exact ``(organizationId, projectId)`` pair returned by Tern; no route
is inferred from a project name, label, current directory, or a partial key.
Checkout paths are canonicalized and verified as repository roots before they
can enter a routing configuration.

The JSON representation is versioned so a future configuration migration can
fail explicitly instead of silently changing dispatch behavior::

    {
      "version": 1,
      "routes": [
        {
          "organizationId": "org_123",
          "projectId": "project_123",
          "checkoutPath": "/home/user/Projects/example",
          "label": "Example"
        }
      ]
    }

This module does not invoke a shell.  Git root discovery uses the argument
vector ``git -C <path> rev-parse --show-toplevel`` with ``shell=False``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


CONFIG_VERSION = 1


class RoutingError(ValueError):
    """Base error for invalid routing configuration or checkout paths."""


class InvalidCheckoutError(RoutingError):
    """Raised when a route checkout is unsafe or is not a repository root."""


class InvalidConfigError(RoutingError):
    """Raised when a routing config is malformed or unsupported."""


class DuplicateRouteError(InvalidConfigError):
    """Raised when two routes claim the same organization/project key."""


class RouteNotFoundError(LookupError):
    """Raised by :meth:`ProjectRouter.require` for an unmapped exact key."""


def _require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidConfigError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise InvalidConfigError(f"{field} must not have surrounding whitespace")
    return value


def _optional_label(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidConfigError("label must be a string when provided")
    label = value.strip()
    if not label:
        raise InvalidConfigError("label must not be empty when provided")
    return label


def _canonical_existing_path(value: str | os.PathLike[str]) -> Path:
    """Return an existing path as an absolute, symlink-resolved directory."""

    try:
        raw = os.fspath(value)
    except TypeError as error:
        raise InvalidCheckoutError("checkoutPath must be a filesystem path") from error
    if not isinstance(raw, str) or not raw:
        raise InvalidCheckoutError("checkoutPath must be a non-empty filesystem path")

    expanded = os.path.expanduser(raw)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        raise InvalidCheckoutError("checkoutPath must be absolute")

    try:
        canonical = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise InvalidCheckoutError("checkoutPath must exist") from error
    if not canonical.is_dir():
        raise InvalidCheckoutError("checkoutPath must be a directory")

    # Do this after resolving symlinks: a symlink to / or to the home
    # directory must be rejected just like the direct path.
    if canonical.parent == canonical:
        raise InvalidCheckoutError("checkoutPath must not be the filesystem root")
    try:
        home = Path.home().resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise InvalidCheckoutError("could not resolve the current user home") from error
    if canonical == home:
        raise InvalidCheckoutError("checkoutPath must not be the user home directory")

    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(canonical), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InvalidCheckoutError("checkoutPath is not a git repository root") from error
    if result.returncode != 0:
        raise InvalidCheckoutError("checkoutPath is not a git repository root")

    output = result.stdout.strip()
    if not output or len(output.splitlines()) != 1:
        raise InvalidCheckoutError("git did not return a repository root")
    try:
        git_root = Path(output).resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise InvalidCheckoutError("git returned an invalid repository root") from error
    if git_root != canonical:
        raise InvalidCheckoutError("checkoutPath must be the exact git repository root")
    return canonical


def validate_checkout_path(value: str | os.PathLike[str]) -> Path:
    """Validate and canonicalize a route checkout path.

    The returned :class:`~pathlib.Path` is absolute and symlink-resolved.  A
    path is accepted only when it is an existing directory and ``git`` reports
    that exact directory as the repository root.  Both regular repositories
    and linked worktrees are supported because Git reports the worktree root
    for ``rev-parse --show-toplevel``.
    """

    return _canonical_existing_path(value)


@dataclass(frozen=True, slots=True)
class ProjectRoute:
    """One exact organization/project mapping to a local checkout."""

    organization_id: str
    project_id: str
    checkout_path: Path
    label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "organization_id", _require_identifier(self.organization_id, "organizationId"))
        object.__setattr__(self, "project_id", _require_identifier(self.project_id, "projectId"))
        object.__setattr__(self, "checkout_path", validate_checkout_path(self.checkout_path))
        object.__setattr__(self, "label", _optional_label(self.label))

    @property
    def key(self) -> tuple[str, str]:
        """The exact lookup key used by :class:`ProjectRouter`."""

        return (self.organization_id, self.project_id)

    @property
    def organizationId(self) -> str:  # noqa: N802 - wire-format convenience
        return self.organization_id

    @property
    def projectId(self) -> str:  # noqa: N802 - wire-format convenience
        return self.project_id

    @property
    def checkoutPath(self) -> Path:  # noqa: N802 - wire-format convenience
        return self.checkout_path

    @property
    def path(self) -> Path:
        """A concise alias for callers that treat routes as path records."""

        return self.checkout_path

    def to_dict(self) -> dict[str, str]:
        """Return the JSON-safe, canonical wire representation."""

        payload: dict[str, str] = {
            "organizationId": self.organization_id,
            "projectId": self.project_id,
            "checkoutPath": os.fspath(self.checkout_path),
        }
        if self.label is not None:
            payload["label"] = self.label
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectRoute":
        if not isinstance(payload, Mapping):
            raise InvalidConfigError("each route must be an object")
        allowed = {"organizationId", "projectId", "checkoutPath", "label"}
        unknown = set(payload) - allowed
        if unknown:
            names = ", ".join(sorted(str(value) for value in unknown))
            raise InvalidConfigError(f"route contains unknown field(s): {names}")
        missing = [field for field in ("organizationId", "projectId", "checkoutPath") if field not in payload]
        if missing:
            raise InvalidConfigError(f"route is missing required field(s): {', '.join(missing)}")
        return cls(
            organization_id=payload["organizationId"],
            project_id=payload["projectId"],
            checkout_path=payload["checkoutPath"],
            label=payload.get("label"),
        )


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """Versioned collection of unique project routes."""

    routes: tuple[ProjectRoute, ...] = ()
    version: int = CONFIG_VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != CONFIG_VERSION:
            raise InvalidConfigError(
                f"unsupported routing config version: {self.version!r} (expected {CONFIG_VERSION})"
            )
        try:
            routes = tuple(self.routes)
        except TypeError as error:
            raise InvalidConfigError("routes must be an iterable of route objects") from error
        index: dict[tuple[str, str], ProjectRoute] = {}
        for route in routes:
            if not isinstance(route, ProjectRoute):
                raise InvalidConfigError("routes must contain ProjectRoute objects")
            if route.key in index:
                raise DuplicateRouteError(
                    "duplicate route for organizationId/projectId "
                    f"{route.organization_id!r}/{route.project_id!r}"
                )
            index[route.key] = route
        object.__setattr__(self, "routes", routes)

    @property
    def route_count(self) -> int:
        return len(self.routes)

    def lookup(self, organization_id: str, project_id: str) -> ProjectRoute | None:
        """Find only an exact key; return ``None`` for every unmapped key."""

        if not isinstance(organization_id, str) or not isinstance(project_id, str):
            return None
        for route in self.routes:
            if route.key == (organization_id, project_id):
                return route
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "routes": [route.to_dict() for route in self.routes]}

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RoutingConfig":
        if not isinstance(payload, Mapping):
            raise InvalidConfigError("routing config must be an object")
        allowed = {"version", "routes"}
        unknown = set(payload) - allowed
        if unknown:
            names = ", ".join(sorted(str(value) for value in unknown))
            raise InvalidConfigError(f"routing config contains unknown field(s): {names}")
        if "version" not in payload:
            raise InvalidConfigError("routing config is missing version")
        if "routes" not in payload:
            raise InvalidConfigError("routing config is missing routes")
        if type(payload["version"]) is not int or payload["version"] != CONFIG_VERSION:
            raise InvalidConfigError(
                f"unsupported routing config version: {payload['version']!r} (expected {CONFIG_VERSION})"
            )
        if not isinstance(payload["routes"], list):
            raise InvalidConfigError("routes must be a JSON array")
        return cls(
            routes=tuple(ProjectRoute.from_dict(route) for route in payload["routes"]),
            version=payload["version"],
        )

    @classmethod
    def from_json(cls, document: str | bytes | bytearray) -> "RoutingConfig":
        try:
            payload = json.loads(document)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidConfigError("routing config is not valid JSON") from error
        return cls.from_dict(payload)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "RoutingConfig":
        try:
            document = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise InvalidConfigError("could not read routing config") from error
        return cls.from_json(document)

    def write(self, path: str | os.PathLike[str], *, indent: int | None = 2) -> None:
        try:
            Path(path).write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise InvalidConfigError("could not write routing config") from error


class ProjectRouter:
    """Immutable exact-key router with no fallback or guessed checkout."""

    def __init__(self, config: RoutingConfig | Iterable[ProjectRoute]) -> None:
        self.config = config if isinstance(config, RoutingConfig) else RoutingConfig(tuple(config))
        self._routes = {route.key: route for route in self.config.routes}

    @classmethod
    def from_json(cls, document: str | bytes | bytearray) -> "ProjectRouter":
        return cls(RoutingConfig.from_json(document))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectRouter":
        return cls(RoutingConfig.from_dict(payload))

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ProjectRouter":
        return cls(RoutingConfig.load(path))

    def lookup(self, organization_id: str, project_id: str) -> ProjectRoute | None:
        """Return a route only for the exact pair, otherwise ``None``."""

        if not isinstance(organization_id, str) or not isinstance(project_id, str):
            return None
        return self._routes.get((organization_id, project_id))

    def require(self, organization_id: str, project_id: str) -> ProjectRoute:
        """Resolve an exact route or fail closed with no default checkout."""

        route = self.lookup(organization_id, project_id)
        if route is None:
            raise RouteNotFoundError(
                f"no checkout route for organizationId/projectId {organization_id!r}/{project_id!r}"
            )
        return route

    def to_dict(self) -> dict[str, Any]:
        return self.config.to_dict()

    def to_json(self, *, indent: int | None = 2) -> str:
        return self.config.to_json(indent=indent)


def parse_config(document: str | bytes | bytearray) -> RoutingConfig:
    """Functional alias for callers that do not need the config class name."""

    return RoutingConfig.from_json(document)


def serialize_config(config: RoutingConfig, *, indent: int | None = 2) -> str:
    if not isinstance(config, RoutingConfig):
        raise TypeError("config must be a RoutingConfig")
    return config.to_json(indent=indent)


def load_config(path: str | os.PathLike[str]) -> RoutingConfig:
    return RoutingConfig.load(path)


def save_config(config: RoutingConfig, path: str | os.PathLike[str], *, indent: int | None = 2) -> None:
    if not isinstance(config, RoutingConfig):
        raise TypeError("config must be a RoutingConfig")
    config.write(path, indent=indent)


# Short aliases keep the module pleasant to use from an adapter while the
# explicit names above make the model boundary self-documenting.
Route = ProjectRoute
Router = ProjectRouter


__all__ = [
    "CONFIG_VERSION",
    "DuplicateRouteError",
    "InvalidCheckoutError",
    "InvalidConfigError",
    "ProjectRoute",
    "ProjectRouter",
    "Route",
    "RouteNotFoundError",
    "Router",
    "RoutingConfig",
    "RoutingError",
    "load_config",
    "parse_config",
    "save_config",
    "serialize_config",
    "validate_checkout_path",
]

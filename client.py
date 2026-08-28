"""Dependency-free client for Tern's provider-neutral runner protocol."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from email.message import Message
from http.client import HTTPResponse
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_BASE_URL = "https://gettern.app/api/runner/v1"
DEFAULT_CAPABILITIES = ("progress", "artifacts")
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_PROTOCOL_CODES = frozenset({"rate_limited", "temporarily_unavailable"})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class RunnerProtocolError(RuntimeError):
    """Bounded runner error that never contains the runner credential."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: str = "internal_error",
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def normalize_base_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Tern runner URL is required")
    parsed = urlparse(raw)
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Tern runner URL cannot contain credentials or a fragment")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("Tern runner URL must use HTTPS (HTTP is allowed only for loopback)")
    if not parsed.netloc:
        raise ValueError("Tern runner URL must be absolute")
    return raw.rstrip("/")


def _default_transport(request: Request, timeout: float, max_bytes: int) -> TransportResponse:
    opener = build_opener(_NoRedirect())
    try:
        response: HTTPResponse = opener.open(request, timeout=timeout)
        status = response.status
        headers: Message = response.headers
        body = response.read(max_bytes + 1)
    except HTTPError as error:
        status = error.code
        headers = error.headers
        body = error.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise RunnerProtocolError(
            "Tern runner response exceeded the configured size limit",
            status=status,
            code="payload_too_large",
        )
    return TransportResponse(status=status, headers=dict(headers.items()), body=body)


class RunnerClient:
    def __init__(
        self,
        base_url: str,
        credential: str,
        *,
        timeout: float = 15.0,
        max_response_bytes: int = 64_000,
        max_retries: int = 3,
        retry_base_seconds: float = 0.25,
        transport: Callable[[Request, float, int], TransportResponse] = _default_transport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        secret = (credential or "").strip()
        if not secret:
            raise ValueError("Tern runner credential is required")
        self.base_url = normalize_base_url(base_url)
        self._credential = secret
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._transport = transport
        self._sleep = sleep

    def _endpoint(self, path: str) -> str:
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    @staticmethod
    def _decode(response: TransportResponse) -> Any:
        if not response.body.strip():
            return {}
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RunnerProtocolError(
                "Tern runner returned invalid JSON",
                status=response.status,
                code="invalid_response",
            ) from error

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    @staticmethod
    def _retry_after(headers: Mapping[str, str], payload: Any) -> float | None:
        header = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
        candidate: Any = header
        unwrapped = RunnerClient._unwrap(payload)
        if candidate is None and isinstance(unwrapped, dict):
            error = unwrapped.get("error", unwrapped)
            if isinstance(error, dict):
                candidate = error.get("retryAfterSeconds")
        try:
            seconds = float(candidate)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(seconds, 60.0))

    def _sanitize_message(self, message: str, body: dict[str, Any] | None = None) -> str:
        sanitized = message.replace(self._credential, "[redacted]")
        if isinstance(body, dict):
            for key in ("leaseToken",):
                value = body.get(key)
                if isinstance(value, str) and value:
                    sanitized = sanitized.replace(value, "[redacted]")
        return sanitized[:4_000]

    def _protocol_error(
        self, response: TransportResponse, payload: Any, body: dict[str, Any] | None
    ) -> RunnerProtocolError:
        unwrapped = RunnerClient._unwrap(payload)
        error = unwrapped.get("error", unwrapped) if isinstance(unwrapped, dict) else {}
        code = error.get("code", "internal_error") if isinstance(error, dict) else "internal_error"
        message = (
            error.get("message")
            if isinstance(error, dict) and isinstance(error.get("message"), str)
            else f"Tern runner request failed with HTTP {response.status}"
        )
        retryable = bool(isinstance(error, dict) and error.get("retryable"))
        retryable = retryable or response.status in RETRYABLE_HTTP_STATUS or code in RETRYABLE_PROTOCOL_CODES
        return RunnerProtocolError(
            self._sanitize_message(message, body),
            status=response.status,
            code=code,
            retryable=retryable,
            retry_after_seconds=RunnerClient._retry_after(response.headers, payload),
        )

    def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        idempotency_key = body.get("idempotencyKey") if isinstance(body, dict) else None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._credential}",
            "User-Agent": "hermes-tern-plugin/0.2.1",
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if isinstance(idempotency_key, str):
            headers["Idempotency-Key"] = idempotency_key

        for attempt in range(self.max_retries + 1):
            request = Request(self._endpoint(path), data=encoded, headers=headers, method=method)
            try:
                response = self._transport(request, self.timeout, self.max_response_bytes)
                if len(response.body) > self.max_response_bytes:
                    raise RunnerProtocolError(
                        "Tern runner response exceeded the configured size limit",
                        status=response.status,
                        code="payload_too_large",
                    )
                payload = self._decode(response)
                if 200 <= response.status < 300:
                    return self._unwrap(payload)
                error = self._protocol_error(response, payload, body)
            except RunnerProtocolError as caught:
                error = caught
            except (OSError, URLError, TimeoutError) as caught:
                error = RunnerProtocolError(
                    f"Tern runner request could not be completed: {type(caught).__name__}",
                    code="transport_error",
                    retryable=True,
                )
            if attempt >= self.max_retries or not error.retryable:
                raise error
            delay = error.retry_after_seconds
            self._sleep(delay if delay is not None else self.retry_base_seconds * (2**attempt))
        raise AssertionError("retry loop terminated unexpectedly")

    def list_ready_runs(
        self,
        *,
        limit: int = 10,
        cursor: str | None = None,
        organization_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if cursor:
            query["cursor"] = cursor
        if organization_id:
            query["organizationId"] = organization_id
        if project_id:
            query["projectId"] = project_id
        result = self.request_json("GET", f"runs?{urlencode(query)}")
        self._expect_operation(result, "list")
        if not isinstance(result, dict) or not isinstance(result.get("runs"), list):
            raise RunnerProtocolError("Tern list response did not contain runs", code="invalid_response")
        return result

    def claim(self, run_id: str, idempotency_key: str) -> dict[str, Any]:
        result = self.request_json(
            "POST",
            f"runs/{run_id}/claim",
            {"runId": run_id, "idempotencyKey": idempotency_key},
        )
        self._expect_operation(result, "claim")
        return result

    def get_context(self, run_id: str, attempt_id: str, lease_token: str) -> dict[str, Any]:
        result = self.request_json(
            "POST",
            f"runs/{run_id}/attempts/{attempt_id}/context",
            {"runId": run_id, "attemptId": attempt_id, "leaseToken": lease_token},
        )
        self._expect_operation(result, "get_context")
        return result

    def renew(
        self,
        run_id: str,
        attempt_id: str,
        lease_token: str,
        idempotency_key: str,
        extension_seconds: int,
    ) -> dict[str, Any]:
        result = self.request_json(
            "POST",
            f"runs/{run_id}/attempts/{attempt_id}/lease",
            {
                "runId": run_id,
                "attemptId": attempt_id,
                "leaseToken": lease_token,
                "idempotencyKey": idempotency_key,
                "extensionSeconds": extension_seconds,
            },
        )
        self._expect_operation(result, "renew_lease")
        return result

    def report(
        self,
        run_id: str,
        attempt_id: str,
        lease_token: str,
        idempotency_key: str,
        capabilities: list[str],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        result = self.request_json(
            "POST",
            f"runs/{run_id}/attempts/{attempt_id}/report",
            {
                "runId": run_id,
                "attemptId": attempt_id,
                "leaseToken": lease_token,
                "idempotencyKey": idempotency_key,
                "capabilities": capabilities,
                "event": event,
            },
        )
        self._expect_operation(result, "report")
        return result

    def finish(
        self,
        run_id: str,
        attempt_id: str,
        lease_token: str,
        idempotency_key: str,
        capabilities: list[str],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        result = self.request_json(
            "POST",
            f"runs/{run_id}/attempts/{attempt_id}/finish",
            {
                "runId": run_id,
                "attemptId": attempt_id,
                "leaseToken": lease_token,
                "idempotencyKey": idempotency_key,
                "capabilities": capabilities,
                "result": result,
            },
        )
        self._expect_operation(result, "finish")
        return result

    @staticmethod
    def _expect_operation(result: Any, expected: str) -> None:
        if isinstance(result, dict) and result.get("operation") not in (None, expected):
            raise RunnerProtocolError(
                f"Tern runner returned {result.get('operation')!r} for a {expected!r} request",
                code="invalid_response",
            )

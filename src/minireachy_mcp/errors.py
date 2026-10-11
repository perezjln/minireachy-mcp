"""Error types raised by the daemon client.

Messages are written for the LLM: they say what happened and what to do next,
without stack traces, secrets or request bodies.
"""

from __future__ import annotations


class DaemonError(Exception):
    """Base class for every failure talking to the MiniReachy daemon."""

    code = "daemon_error"

    def __init__(self, message: str, *, method: str = "", path: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.method = method
        self.path = path

    def to_llm(self) -> str:
        where = f" ({self.method} {self.path})" if self.method else ""
        return f"[{self.code}]{where} {self.message}"


class DaemonUnreachableError(DaemonError):
    """The TCP connection could not be established. The request was NOT delivered."""

    code = "daemon_unreachable"


class DaemonTimeoutError(DaemonError):
    """A read-only request timed out (safe to retry)."""

    code = "daemon_timeout"


class UncertainOutcomeError(DaemonError):
    """A state-changing request was sent but no conclusive answer was received.

    The daemon may or may not have executed it. It is never retried automatically.
    """

    code = "uncertain_outcome"


class DaemonHTTPError(DaemonError):
    """The daemon answered with a non-2xx status."""

    code = "daemon_http_error"

    def __init__(self, message: str, *, status: int, detail: object = None, method: str = "", path: str = "") -> None:
        super().__init__(message, method=method, path=path)
        self.status = status
        self.detail = detail


class DaemonValidationError(DaemonHTTPError):
    """HTTP 422: the daemon rejected the parameters."""

    code = "daemon_validation_error"


class DaemonAuthError(DaemonHTTPError):
    """HTTP 401/403."""

    code = "daemon_auth_error"


class InvalidResponseError(DaemonError):
    """The daemon answered 2xx but the payload is not what the API documents."""

    code = "invalid_response"


class ResponseTooLargeError(DaemonError):
    """The response exceeded the configured size limit and was discarded."""

    code = "response_too_large"


class SafetyGateError(Exception):
    """A tool was refused by the server's safety configuration. Nothing was sent."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_llm(self) -> str:
        return f"[refused_by_safety_policy] {self.message} No request was sent to the robot."

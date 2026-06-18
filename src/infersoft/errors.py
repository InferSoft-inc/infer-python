"""Exception hierarchy for the Infersoft SDK."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .models import CreditsEstimate, Job, UploadResult


class InfersoftError(Exception):
    """Base class for every error raised by this SDK."""


class AuthenticationError(InfersoftError):
    """Raised when an OAuth2 access token could not be obtained."""


class APIConnectionError(InfersoftError):
    """Raised when the request could not reach the API.

    Covers connection failures, resets, and protocol errors — i.e. no HTTP
    response was received. The underlying ``httpx`` error is the ``__cause__``.
    Following the OpenAI/Anthropic convention, ``APITimeoutError`` is a subclass.
    """


class APITimeoutError(APIConnectionError):
    """Raised when a request exceeds its timeout (a kind of connection error)."""


class WaitTimeoutError(InfersoftError):
    """Raised when a ``wait`` helper exceeds its ``max_wait_seconds`` budget."""


class CreditsLimitExceededError(InfersoftError):
    """Raised by ``jobs.run(max_credits=...)`` when the estimate exceeds the budget.

    The job is **not** started. The offending estimate is available as
    ``.estimate`` (so the caller can inspect ``total_credits`` / ``page_count``
    or still start it explicitly via ``jobs.start(estimate.id)``).
    """

    def __init__(self, estimate: CreditsEstimate, max_credits: int) -> None:
        super().__init__(
            f"estimated cost of {estimate.total_credits} credits exceeds "
            f"max_credits={max_credits}; job not started"
        )
        self.estimate = estimate
        self.max_credits = max_credits


class UploadManyError(InfersoftError):
    """Raised when an ``upload_many`` batch fails partway through.

    Earlier batches are **not** rolled back: ``.partial`` aggregates everything
    uploaded before the failure and ``.batches_completed`` counts the batches
    that succeeded. The failing batch's original error is the ``__cause__``.
    """

    def __init__(
        self, message: str, *, partial: UploadResult, batches_completed: int
    ) -> None:
        super().__init__(message)
        self.partial = partial
        self.batches_completed = batches_completed


class ExtractError(InfersoftError):
    """Raised when the ``client.extract`` pipeline cannot complete.

    Carries whatever partial state exists so the caller can resume manually:
    ``.upload`` (set when local files were uploaded — those documents are NOT
    rolled back) and ``.job`` (set when an extraction job was started; resume
    with ``jobs.wait(error.job)`` / ``jobs.results(error.job)``).
    """

    def __init__(
        self,
        message: str,
        *,
        job: Job | None = None,
        upload: UploadResult | None = None,
    ) -> None:
        super().__init__(message)
        self.job = job
        self.upload = upload


class UploadTransferError(InfersoftError):
    """Raised when uploading file bytes to a presigned URL fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class DownloadTransferError(InfersoftError):
    """Raised when fetching file bytes from a signed download URL fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class APIError(InfersoftError):
    """Raised for non-2xx responses from the Infersoft API.

    Carries the parsed RFC 7807 problem details when present, plus the
    ``request_id`` from the response headers (when the server provides one) for
    support and tracing.
    """

    def __init__(
        self,
        status_code: int,
        *,
        title: str,
        detail: str | None = None,
        type: str | None = None,
        instance: str | None = None,
        request_id: str | None = None,
        response: httpx.Response | None = None,
    ) -> None:
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.type = type
        self.instance = instance
        self.request_id = request_id
        self.response = response
        message = f"{status_code} {title}"
        if detail:
            message += f": {detail}"
        if request_id:
            message += f" (request_id={request_id})"
        super().__init__(message)


class BadRequestError(APIError):
    """400"""


class UnauthorizedError(APIError):
    """401"""


class ForbiddenError(APIError):
    """403"""


class NotFoundError(APIError):
    """404"""


class ConflictError(APIError):
    """409"""


class PayloadTooLargeError(APIError):
    """413"""


class PreconditionFailedError(APIError):
    """412"""


class UnprocessableEntityError(APIError):
    """422 — e.g. reusing an Idempotency-Key with different request parameters."""


class RateLimitError(APIError):
    """429"""


class ServerError(APIError):
    """5xx"""


# Keys are typed ``int`` (HTTPStatus members are ints) so lookups by a raw
# ``response.status_code`` type-check cleanly.
_STATUS_TO_ERROR: dict[int, type[APIError]] = {
    HTTPStatus.BAD_REQUEST: BadRequestError,
    HTTPStatus.UNAUTHORIZED: UnauthorizedError,
    HTTPStatus.FORBIDDEN: ForbiddenError,
    HTTPStatus.NOT_FOUND: NotFoundError,
    HTTPStatus.CONFLICT: ConflictError,
    HTTPStatus.PRECONDITION_FAILED: PreconditionFailedError,
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: PayloadTooLargeError,
    HTTPStatus.UNPROCESSABLE_ENTITY: UnprocessableEntityError,
    HTTPStatus.TOO_MANY_REQUESTS: RateLimitError,
}

#: Headers checked, in order, for a server-issued request/trace identifier.
_REQUEST_ID_HEADERS = (
    "x-request-id",
    "x-amzn-requestid",
    "x-amz-request-id",
    "x-correlation-id",
)


def request_id_of(response: httpx.Response) -> str | None:
    """Return the first request/trace id found in the response headers, if any."""
    for header in _REQUEST_ID_HEADERS:
        value = response.headers.get(header)
        if value:
            return str(value)
    return None


def raise_for_problem(response: httpx.Response) -> None:
    """Inspect a failed response and raise the most specific APIError subclass."""
    title = response.reason_phrase or "Error"
    detail = type_ = instance = None

    try:
        body = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        title = body.get("title") or title
        detail = body.get("detail")
        type_ = body.get("type")
        instance = body.get("instance")

    cls = _STATUS_TO_ERROR.get(response.status_code)
    if cls is None:
        cls = ServerError if response.is_server_error else APIError

    raise cls(
        response.status_code,
        title=title,
        detail=detail,
        type=type_,
        instance=instance,
        request_id=request_id_of(response),
        response=response,
    )

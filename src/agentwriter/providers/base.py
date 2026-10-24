"""Provider boundaries shared by the worker, fixture tests, and integrations."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


class ProviderError(Exception):
    """A public-safe classification without upstream bodies or credentials.

    The scheduler owns retry decisions. ``outcome_unknown`` means a request may
    have completed remotely; callers must retain/reconcile its reservation.
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True, slots=True)
class ModelResult[T: BaseModel]:
    data: T
    requested_model: str
    resolved_model: str
    request_id: str | None = None
    provider: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: Decimal | None = None
    duration_seconds: float = 0.0
    fixture: bool = False


class ModelGateway(Protocol):
    async def generate(
        self,
        schema: type[ModelT],
        messages: list[dict[str, str]],
        call_identity: str,
        max_tokens: int = 2048,
        *,
        reserved_cost_usd: Decimal | None = None,
    ) -> ModelResult[ModelT]: ...

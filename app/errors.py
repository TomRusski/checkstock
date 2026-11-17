"""Доменные ошибки. Все они превращаются в аккуратный JSON вида {"error": {...}}."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class NotFoundError(DomainError):
    status_code = 404
    code = "NOT_FOUND"


class ValidationFailed(DomainError):
    status_code = 422
    code = "VALIDATION_FAILED"


class ConflictError(DomainError):
    status_code = 409
    code = "CONFLICT"


class InsufficientStock(ConflictError):
    """Нехватка на конкретном ФФ.

    В details всегда лежит раскладка: сколько нужно, сколько есть на выбранном ФФ,
    сколько всего по сети и что можно сделать (переместить / разбить отгрузку).
    """

    code = "INSUFFICIENT_STOCK"


class AccountingError(DomainError):
    """Нарушение инварианта учёта. Такого быть не должно — это баг, а не ввод пользователя."""

    status_code = 500
    code = "ACCOUNTING_INVARIANT_BROKEN"

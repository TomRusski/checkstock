"""Прикладные операции над документами: приход, перемещение, FBS, FBO.

Всё работает в одной транзакции HTTP-запроса: либо документ создан/переведён
целиком вместе с проводками, либо не произошло ничего.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.allocation import allocate, analyze_shortage, ensure_available
from app.catalog import default_shop, get_warehouse
from app.documents import (
    create_document,
    replay_by_key,
    resolve_lines,
    transition,
)
from app.errors import InsufficientStock
from app.models import DocKind, DocStatus, Product
from app.serializers import document_payload


def create_receipt(
    session: Session, payload: dict[str, Any], idempotency_key: str | None = None
) -> tuple[dict[str, Any], bool]:
    replay = replay_by_key(session, idempotency_key)
    if replay is not None:
        return replay, True
    lines = resolve_lines(session, payload["lines"])
    doc, event = create_document(
        session,
        kind=DocKind.RECEIPT.value,
        shop_id=default_shop(session).id,
        lines=lines,
        to_warehouse_id=payload["to_warehouse_id"],
        reference=payload.get("reference"),
        note=payload.get("note"),
        idempotency_key=idempotency_key,
    )
    data = document_payload(session, doc)
    event.response_snapshot = data
    session.flush()
    return data, False


def create_transfer(
    session: Session, payload: dict[str, Any], idempotency_key: str | None = None
) -> tuple[dict[str, Any], bool]:
    replay = replay_by_key(session, idempotency_key)
    if replay is not None:
        return replay, True
    lines = resolve_lines(session, payload["lines"])
    doc, event = create_document(
        session,
        kind=DocKind.TRANSFER.value,
        shop_id=default_shop(session).id,
        lines=lines,
        from_warehouse_id=payload["from_warehouse_id"],
        to_warehouse_id=payload["to_warehouse_id"],
        reference=payload.get("reference"),
        note=payload.get("note"),
        idempotency_key=idempotency_key,
    )
    data = document_payload(session, doc)
    event.response_snapshot = data
    session.flush()
    return data, False


def quick_transfer(
    session: Session, payload: dict[str, Any], idempotency_key: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Перемещение «под ключ»: создать + отправить + принять одной транзакцией.

    Нужно для сценария нехватки: менеджеру важно закрыть дефицит одним действием,
    но в учёте это всё равно три честных события с проводками.
    """
    replay = replay_by_key(session, idempotency_key)
    if replay is not None:
        return replay, True
    data, _ = create_transfer(session, payload, idempotency_key=idempotency_key)
    doc_id = data["id"]
    transition(session, doc_id, DocStatus.SHIPPED.value, expected_kind=DocKind.TRANSFER.value)
    result = transition(
        session, doc_id, DocStatus.RECEIVED.value, expected_kind=DocKind.TRANSFER.value
    )
    return result.payload, False


def create_fbo(
    session: Session, payload: dict[str, Any], idempotency_key: str | None = None
) -> tuple[dict[str, Any], bool]:
    replay = replay_by_key(session, idempotency_key)
    if replay is not None:
        return replay, True
    lines = resolve_lines(session, payload["lines"])
    doc, event = create_document(
        session,
        kind=DocKind.FBO.value,
        shop_id=default_shop(session).id,
        lines=lines,
        from_warehouse_id=payload["from_warehouse_id"],
        reference=payload.get("reference"),
        note=payload.get("note"),
        idempotency_key=idempotency_key,
    )
    data = document_payload(session, doc)
    event.response_snapshot = data
    session.flush()
    return data, False


def create_fbs(
    session: Session, payload: dict[str, Any], idempotency_key: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Резерв FBS. Два режима:

    * allow_split=False (по умолчанию): товар берём только с выбранного ФФ,
      при нехватке — 409 с раскладкой и вариантами решения;
    * allow_split=True: раскладываем заказ по складам (сначала выбранный ФФ)
      и создаём несколько отправлений с одним номером заказа.
    """
    replay = replay_by_key(session, idempotency_key)
    if replay is not None:
        return replay, True

    lines = resolve_lines(session, payload["lines"])
    from_id = payload["from_warehouse_id"]
    get_warehouse(session, from_id)  # существование проверяем до расчётов
    shop_id = default_shop(session).id
    reference = payload.get("reference")
    note = payload.get("note")
    allow_split = bool(payload.get("allow_split", False))

    if not allow_split:
        ensure_available(session, from_id, lines)
        doc, event = create_document(
            session,
            kind=DocKind.FBS.value,
            shop_id=shop_id,
            lines=lines,
            from_warehouse_id=from_id,
            reference=reference,
            note=note,
            idempotency_key=idempotency_key,
        )
        data = {"split": False, "documents": [document_payload(session, doc)]}
        event.response_snapshot = data
        session.flush()
        return data, False

    per_warehouse: dict[int, list[tuple[Product, int]]] = {}
    for product, qty in lines:
        plan = allocate(session, product.id, from_id, qty)
        if not plan:
            details = analyze_shortage(session, product, from_id, qty)
            raise InsufficientStock(
                f"Товара {product.sku} не хватает даже по всем складам: "
                f"нужно {qty}, доступно {details['total_available']}",
                details,
            )
        for wh_id, take in plan:
            per_warehouse.setdefault(wh_id, []).append((product, take))

    docs = []
    key_event = None
    for wh_id in sorted(per_warehouse, key=lambda w: (w != from_id, w)):
        doc, event = create_document(
            session,
            kind=DocKind.FBS.value,
            shop_id=shop_id,
            lines=per_warehouse[wh_id],
            from_warehouse_id=wh_id,
            reference=reference,
            note=note,
            idempotency_key=idempotency_key if key_event is None else None,
        )
        key_event = key_event or event
        docs.append(document_payload(session, doc))

    data = {"split": len(docs) > 1, "documents": docs}
    if key_event is not None:
        key_event.response_snapshot = data
    session.flush()
    return data, False

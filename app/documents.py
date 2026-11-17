"""Документы движения: конечный автомат + проводки для каждого перехода.

Это единственное место, где меняется статус документа и пишутся проводки.
Все сервисы (приход, перемещение, FBS, FBO) работают через эти функции,
поэтому правила идемпотентности и сходимости одинаковы для всех операций.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.accounting import Posting, apply_postings
from app.errors import ConflictError, NotFoundError, ValidationFailed
from app.locations import singleton_location, transit_location, warehouse_location
from app.models import (
    Bucket,
    DocKind,
    DocStatus,
    Document,
    DocumentEvent,
    DocumentLine,
    LocationKind,
    Product,
    Warehouse,
)
from app.serializers import document_payload

# Разрешённые переходы. Всё, что не перечислено, — запрещено (409).
TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    (DocKind.RECEIPT.value, DocStatus.DRAFT.value): (DocStatus.POSTED.value,),
    (DocKind.TRANSFER.value, DocStatus.DRAFT.value): (
        DocStatus.SHIPPED.value,
        DocStatus.CANCELLED.value,
    ),
    (DocKind.TRANSFER.value, DocStatus.SHIPPED.value): (DocStatus.RECEIVED.value,),
    (DocKind.FBS.value, DocStatus.RESERVED.value): (
        DocStatus.SHIPPED.value,
        DocStatus.CANCELLED.value,
    ),
    (DocKind.FBS.value, DocStatus.SHIPPED.value): (DocStatus.DELIVERED.value,),
    (DocKind.FBO.value, DocStatus.DRAFT.value): (
        DocStatus.SHIPPED.value,
        DocStatus.CANCELLED.value,
    ),
    (DocKind.FBO.value, DocStatus.SHIPPED.value): (DocStatus.RECEIVED.value,),
}

INITIAL_STATUS = {
    DocKind.RECEIPT.value: DocStatus.DRAFT.value,
    DocKind.TRANSFER.value: DocStatus.DRAFT.value,
    DocKind.FBO.value: DocStatus.DRAFT.value,
    # FBS создаётся сразу как резерв: заказ клиента блокирует товар на ФФ.
    DocKind.FBS.value: DocStatus.RESERVED.value,
}


def allowed_transitions(kind: str, status: str) -> tuple[str, ...]:
    return TRANSITIONS.get((kind, status), ())


@dataclass(slots=True)
class TransitionResult:
    payload: dict[str, Any]
    replayed: bool = False


def replay_by_key(session: Session, key: str | None) -> dict[str, Any] | None:
    """Ответ, сохранённый для этого Idempotency-Key (если запрос уже выполнялся)."""
    if not key:
        return None
    event = session.scalar(select(DocumentEvent).where(DocumentEvent.idempotency_key == key))
    return event.response_snapshot if event is not None else None


def resolve_lines(session: Session, lines: list[dict[str, Any]]) -> list[tuple[Product, int]]:
    """Строки документа: артикул можно передать как sku или как product_id."""
    resolved: list[tuple[Product, int]] = []
    seen: set[int] = set()
    for raw in lines:
        sku = raw.get("sku")
        product_id = raw.get("product_id")
        qty = int(raw["qty"])
        if qty <= 0:
            raise ValidationFailed("Количество должно быть больше нуля", {"qty": qty})
        if bool(sku) == bool(product_id):
            raise ValidationFailed("Укажите либо sku, либо product_id (что-то одно)")
        product = (
            session.get(Product, product_id)
            if product_id
            else session.scalar(select(Product).where(Product.sku == sku))
        )
        if product is None:
            raise NotFoundError(f"Артикул {sku or product_id} не найден")
        if product.id in seen:
            raise ValidationFailed(f"Артикул {product.sku} повторяется в документе")
        seen.add(product.id)
        resolved.append((product, qty))
    if not resolved:
        raise ValidationFailed("Документ без строк")
    return resolved


def _get_warehouse(session: Session, warehouse_id: int, role: str) -> Warehouse:
    wh = session.get(Warehouse, warehouse_id)
    if wh is None:
        raise NotFoundError(f"Склад #{warehouse_id} не найден ({role})")
    if wh.is_archived:
        raise ConflictError(
            f"Склад «{wh.name}» в архиве: новые операции на нём запрещены",
            {"warehouse_id": wh.id, "archived": True},
        )
    return wh


def validate_route(session: Session, kind: str, from_id: int | None, to_id: int | None) -> None:
    if kind == DocKind.RECEIPT.value:
        if not to_id:
            raise ValidationFailed("Для прихода нужен склад получателя (to_warehouse_id)")
        _get_warehouse(session, to_id, "получатель")
    elif kind == DocKind.TRANSFER.value:
        if not from_id or not to_id:
            raise ValidationFailed("Для перемещения нужны оба склада")
        if from_id == to_id:
            raise ValidationFailed("Склад отправителя и получателя совпадают")
        _get_warehouse(session, from_id, "отправитель")
        _get_warehouse(session, to_id, "получатель")
    else:  # FBS / FBO
        if not from_id:
            raise ValidationFailed("Нужен склад отправителя (from_warehouse_id)")
        _get_warehouse(session, from_id, "отправитель")


def build_postings(session: Session, doc: Document, to_status: str) -> list[Posting]:
    """Проводки для перехода. Каждая пара «−q / +q» — перекладывание товара."""
    kind, status = doc.kind, to_status
    postings: list[Posting] = []

    def add(product_id: int, location_id: int, bucket: Bucket, delta: int) -> None:
        postings.append(Posting(product_id, location_id, bucket, delta))

    if kind == DocKind.RECEIPT.value and status == DocStatus.POSTED.value:
        supplier = singleton_location(session, LocationKind.SUPPLIER)
        dest = warehouse_location(session, doc.to_warehouse_id)
        for line in doc.lines:
            add(line.product_id, supplier.id, Bucket.FREE, -line.qty)
            add(line.product_id, dest.id, Bucket.FREE, +line.qty)

    elif kind == DocKind.TRANSFER.value and status == DocStatus.SHIPPED.value:
        src = warehouse_location(session, doc.from_warehouse_id)
        transit = transit_location(session, doc)
        for line in doc.lines:
            add(line.product_id, src.id, Bucket.FREE, -line.qty)
            add(line.product_id, transit.id, Bucket.FREE, +line.qty)

    elif kind == DocKind.TRANSFER.value and status == DocStatus.RECEIVED.value:
        transit = transit_location(session, doc)
        dest = warehouse_location(session, doc.to_warehouse_id)
        for line in doc.lines:
            add(line.product_id, transit.id, Bucket.FREE, -line.qty)
            add(line.product_id, dest.id, Bucket.FREE, +line.qty)

    elif kind == DocKind.FBS.value and status == DocStatus.RESERVED.value:
        # Резерв не уводит товар со склада: он переезжает из «доступно» в «резерв».
        wh = warehouse_location(session, doc.from_warehouse_id)
        for line in doc.lines:
            add(line.product_id, wh.id, Bucket.FREE, -line.qty)
            add(line.product_id, wh.id, Bucket.RESERVED, +line.qty)

    elif kind == DocKind.FBS.value and status == DocStatus.SHIPPED.value:
        wh = warehouse_location(session, doc.from_warehouse_id)
        transit = transit_location(session, doc)
        for line in doc.lines:
            add(line.product_id, wh.id, Bucket.RESERVED, -line.qty)
            add(line.product_id, transit.id, Bucket.FREE, +line.qty)

    elif kind == DocKind.FBS.value and status == DocStatus.DELIVERED.value:
        transit = transit_location(session, doc)
        client = singleton_location(session, LocationKind.CLIENT)
        for line in doc.lines:
            add(line.product_id, transit.id, Bucket.FREE, -line.qty)
            add(line.product_id, client.id, Bucket.FREE, +line.qty)

    elif kind == DocKind.FBS.value and status == DocStatus.CANCELLED.value:
        # Отмена резерва возвращает товар в «доступно» на том же ФФ.
        wh = warehouse_location(session, doc.from_warehouse_id)
        for line in doc.lines:
            add(line.product_id, wh.id, Bucket.RESERVED, -line.qty)
            add(line.product_id, wh.id, Bucket.FREE, +line.qty)

    elif kind == DocKind.FBO.value and status == DocStatus.SHIPPED.value:
        wh = warehouse_location(session, doc.from_warehouse_id)
        transit = transit_location(session, doc)
        for line in doc.lines:
            add(line.product_id, wh.id, Bucket.FREE, -line.qty)
            add(line.product_id, transit.id, Bucket.FREE, +line.qty)

    elif kind == DocKind.FBO.value and status == DocStatus.RECEIVED.value:
        transit = transit_location(session, doc)
        marketplace = singleton_location(session, LocationKind.MARKETPLACE)
        for line in doc.lines:
            add(line.product_id, transit.id, Bucket.FREE, -line.qty)
            add(line.product_id, marketplace.id, Bucket.FREE, +line.qty)

    # Черновики и отмена черновика проводок не делают — товар ещё не двигался.
    return postings


def lock_document(session: Session, document_id: int) -> Document:
    doc = session.execute(
        select(Document)
        .where(Document.id == document_id)
        .options(selectinload(Document.lines).selectinload(DocumentLine.product))
        .with_for_update()
    ).scalar_one_or_none()
    if doc is None:
        raise NotFoundError(f"Документ #{document_id} не найден")
    return doc


def create_document(
    session: Session,
    *,
    kind: str,
    shop_id: int,
    lines: list[tuple[Product, int]],
    from_warehouse_id: int | None = None,
    to_warehouse_id: int | None = None,
    reference: str | None = None,
    note: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[Document, DocumentEvent]:
    validate_route(session, kind, from_warehouse_id, to_warehouse_id)
    status = INITIAL_STATUS[kind]
    doc = Document(
        kind=kind,
        status=status,
        shop_id=shop_id,
        from_warehouse_id=from_warehouse_id,
        to_warehouse_id=to_warehouse_id,
        reference=reference,
        note=note,
    )
    doc.lines = [DocumentLine(product_id=p.id, qty=q) for p, q in lines]
    session.add(doc)
    session.flush()

    event = DocumentEvent(
        document_id=doc.id,
        from_status=None,
        to_status=status,
        idempotency_key=idempotency_key,
    )
    session.add(event)
    session.flush()
    apply_postings(session, event.id, build_postings(session, doc, status))
    return doc, event


def transition(
    session: Session,
    document_id: int,
    to_status: str,
    *,
    idempotency_key: str | None = None,
    expected_kind: str | None = None,
) -> TransitionResult:
    """Перевести документ в новый статус ровно один раз.

    Защита от двойного списания работает на двух уровнях:
    * Idempotency-Key — повтор возвращает сохранённый ответ (replayed=True);
    * конечный автомат — повтор без ключа получает 409, проводки не пишутся.
    """
    replay = replay_by_key(session, idempotency_key)
    if replay is not None:
        return TransitionResult(payload=replay, replayed=True)

    doc = lock_document(session, document_id)
    if expected_kind and doc.kind != expected_kind:
        raise NotFoundError(f"Документ #{document_id} не является {expected_kind}")

    allowed = allowed_transitions(doc.kind, doc.status)
    if to_status not in allowed:
        raise ConflictError(
            f"Документ #{doc.id} уже в статусе «{doc.status}»: переход в «{to_status}» невозможен",
            {
                "document_id": doc.id,
                "current_status": doc.status,
                "requested_status": to_status,
                "allowed": list(allowed),
            },
        )

    # Отправка перемещения/FBO списывает «доступно»: проверяем заранее,
    # чтобы вернуть не сухую ошибку, а раскладку по складам и план действий.
    if to_status == DocStatus.SHIPPED.value and doc.kind in (
        DocKind.TRANSFER.value,
        DocKind.FBO.value,
    ):
        from app.allocation import ensure_available

        ensure_available(
            session, doc.from_warehouse_id, [(line.product, line.qty) for line in doc.lines]
        )

    event = DocumentEvent(
        document_id=doc.id,
        from_status=doc.status,
        to_status=to_status,
        idempotency_key=idempotency_key,
    )
    session.add(event)
    session.flush()
    apply_postings(session, event.id, build_postings(session, doc, to_status))

    doc.status = to_status
    session.flush()
    payload = document_payload(session, doc)
    event.response_snapshot = payload
    session.flush()
    return TransitionResult(payload=payload, replayed=False)

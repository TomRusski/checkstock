"""Хелперы для проверок остатков. Читаем столбцы, а не ORM-объекты,
чтобы всегда видеть актуальные закоммиченные данные."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.invariants import check_all
from app.models import (
    Bucket,
    Document,
    DocumentLine,
    LedgerEntry,
    Location,
    LocationKind,
    Product,
    StockBalance,
    Warehouse,
)


def _qty(
    session: Session, sku: str, *, kind: str, bucket: str | None = None, wh: str | None = None
):
    stmt = (
        select(func.coalesce(func.sum(StockBalance.qty), 0))
        .join(Location, Location.id == StockBalance.location_id)
        .join(Product, Product.id == StockBalance.product_id)
        .where(Product.sku == sku, Location.kind == kind)
    )
    if bucket:
        stmt = stmt.where(StockBalance.bucket == bucket)
    if wh:
        stmt = stmt.join(Warehouse, Warehouse.id == Location.warehouse_id).where(
            Warehouse.name == wh
        )
    return int(session.scalar(stmt) or 0)


def available(session: Session, sku: str, wh: str) -> int:
    return _qty(session, sku, kind=LocationKind.FULFILLMENT.value, bucket=Bucket.FREE.value, wh=wh)


def reserved(session: Session, sku: str, wh: str) -> int:
    return _qty(
        session, sku, kind=LocationKind.FULFILLMENT.value, bucket=Bucket.RESERVED.value, wh=wh
    )


def on_hand(session: Session, sku: str, wh: str) -> int:
    return available(session, sku, wh) + reserved(session, sku, wh)


def in_transit(session: Session, sku: str, doc_kind: str | None = None) -> int:
    stmt = (
        select(func.coalesce(func.sum(StockBalance.qty), 0))
        .join(Location, Location.id == StockBalance.location_id)
        .join(Product, Product.id == StockBalance.product_id)
        .outerjoin(Document, Document.id == Location.document_id)
        .where(Product.sku == sku, Location.kind == LocationKind.TRANSIT.value)
    )
    if doc_kind:
        stmt = stmt.where(Document.kind == doc_kind)
    return int(session.scalar(stmt) or 0)


def marketplace(session: Session, sku: str) -> int:
    return _qty(session, sku, kind=LocationKind.MARKETPLACE.value)


def delivered(session: Session, sku: str) -> int:
    return _qty(session, sku, kind=LocationKind.CLIENT.value)


def received(session: Session, sku: str) -> int:
    return -_qty(session, sku, kind=LocationKind.SUPPLIER.value)


def total_everywhere(session: Session, sku: str) -> int:
    """Сколько всего числится по артикулу (ФФ + путь + МП + клиент)."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(StockBalance.qty), 0))
            .join(Location, Location.id == StockBalance.location_id)
            .join(Product, Product.id == StockBalance.product_id)
            .where(
                Product.sku == sku,
                Location.kind != LocationKind.SUPPLIER.value,
            )
        )
        or 0
    )


def assert_invariants(session: Session) -> None:
    report = check_all(session)
    broken = [c for c in report["checks"] if not c["ok"]]
    assert report["ok"], f"Нарушены инварианты: {broken}"


def assert_conserved(session: Session, sku: str, expected_received: int | None = None) -> None:
    """Приход = всё, что где-либо числится. Товар не теряется и не удваивается."""
    total = total_everywhere(session, sku)
    got = received(session, sku)
    assert total == got, f"{sku}: числится {total}, а принято {got}"
    if expected_received is not None:
        assert got == expected_received


def postings_count(session: Session, document_id: int) -> int:
    from app.models import DocumentEvent

    return int(
        session.scalar(
            select(func.count(LedgerEntry.id))
            .join(DocumentEvent, DocumentEvent.id == LedgerEntry.event_id)
            .where(DocumentEvent.document_id == document_id)
        )
        or 0
    )


def events_count(session: Session, document_id: int, to_status: str | None = None) -> int:
    from app.models import DocumentEvent

    stmt = select(func.count(DocumentEvent.id)).where(DocumentEvent.document_id == document_id)
    if to_status:
        stmt = stmt.where(DocumentEvent.to_status == to_status)
    return int(session.scalar(stmt) or 0)


def line_qty(session: Session, document_id: int, sku: str) -> int:
    return int(
        session.scalar(
            select(DocumentLine.qty)
            .join(Product, Product.id == DocumentLine.product_id)
            .where(DocumentLine.document_id == document_id, Product.sku == sku)
        )
        or 0
    )

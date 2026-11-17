"""Учётное ядро: двойная запись, проекция, гарантии на уровне БД."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.accounting import Posting, apply_postings, rebuild_projection
from app.errors import AccountingError, InsufficientStock
from app.locations import singleton_location, warehouse_location
from app.models import Bucket, DocumentEvent, LedgerEntry, LocationKind, Product, StockBalance
from tests.helpers import assert_invariants, available


def _product(session, sku="SKU-101") -> Product:
    return session.scalar(select(Product).where(Product.sku == sku))


def _event(session) -> DocumentEvent:
    from app.models import DocKind, DocStatus, Document

    doc = session.scalar(select(Document).where(Document.kind == DocKind.RECEIPT.value).limit(1))
    assert doc is not None
    event = DocumentEvent(
        document_id=doc.id, from_status=DocStatus.POSTED.value, to_status="posted"
    )
    session.add(event)
    session.flush()
    return event


def test_unbalanced_postings_rejected(session):
    """Нельзя списать товар «в никуда»: сумма проводок по артикулу обязана быть 0."""
    product = _product(session)
    wh = warehouse_location(session, 1)
    event = _event(session)
    with pytest.raises(AccountingError):
        apply_postings(session, event.id, [Posting(product.id, wh.id, Bucket.FREE, -1)])
    session.rollback()


def test_zero_delta_rejected(session):
    product = _product(session)
    wh = warehouse_location(session, 1)
    event = _event(session)
    with pytest.raises(AccountingError):
        apply_postings(
            session,
            event.id,
            [
                Posting(product.id, wh.id, Bucket.FREE, 0),
                Posting(product.id, wh.id, Bucket.RESERVED, 0),
            ],
        )
    session.rollback()


def test_cannot_overdraw_warehouse(session):
    product = _product(session)
    wh = warehouse_location(session, 1)
    supplier = singleton_location(session, LocationKind.SUPPLIER)
    event = _event(session)
    with pytest.raises(InsufficientStock):
        apply_postings(
            session,
            event.id,
            [
                Posting(product.id, wh.id, Bucket.FREE, -1000),
                Posting(product.id, supplier.id, Bucket.FREE, 1000),
            ],
        )
    session.rollback()
    assert available(session, "SKU-101", "Север") == 10


def test_projection_can_be_rebuilt_from_ledger(session):
    """Остатки — именно проекция журнала: удаляем проекцию и собираем заново."""
    before = session.execute(
        select(
            StockBalance.product_id, StockBalance.location_id, StockBalance.bucket, StockBalance.qty
        )
    ).all()
    rebuild_projection(session)
    after = session.execute(
        select(
            StockBalance.product_id, StockBalance.location_id, StockBalance.bucket, StockBalance.qty
        )
    ).all()
    assert sorted(before) == sorted(after)
    assert_invariants(session)


def test_ledger_is_append_only_at_db_level(session):
    """Даже прямой SQL не может изменить или удалить проводку."""
    entry_id = session.scalar(select(LedgerEntry.id).limit(1))
    with pytest.raises(DBAPIError):
        session.execute(
            text("UPDATE ledger_entries SET qty_delta = 999 WHERE id = :id"), {"id": entry_id}
        )
    session.rollback()
    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM ledger_entries WHERE id = :id"), {"id": entry_id})
    session.rollback()


def test_negative_balance_blocked_by_trigger(session):
    """Страховка в БД: минус на складе невозможен даже без кода приложения."""
    product = _product(session)
    wh = warehouse_location(session, 1)
    with pytest.raises(DBAPIError):
        session.execute(
            text(
                "UPDATE stock_balances SET qty = -5 "
                "WHERE product_id = :p AND location_id = :l AND bucket = 'free'"
            ),
            {"p": product.id, "l": wh.id},
        )
    session.rollback()
    assert available(session, "SKU-101", "Север") == 10


def test_supplier_may_go_negative(session):
    """Поставщик — источник: его минус это ровно сумма прихода."""
    supplier = singleton_location(session, LocationKind.SUPPLIER)
    total = session.scalar(
        select(StockBalance.qty).where(
            StockBalance.location_id == supplier.id, StockBalance.bucket == Bucket.FREE.value
        )
    )
    assert total is None or total <= 0
    assert_invariants(session)

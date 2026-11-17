"""Проверки сходимости учёта. Один вызов — пять инвариантов.

Вызывается из GET /api/invariants, из UI (индикатор в шапке) и из тестов
после каждого действия. Если хотя бы одна проверка красная — в учёте баг.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Bucket,
    DocKind,
    DocStatus,
    Document,
    DocumentLine,
    LedgerEntry,
    Location,
    LocationKind,
    Product,
    StockBalance,
    Warehouse,
)


def _check(name: str, description: str, violations: list[Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "ok": not violations,
        "violations": violations,
    }


def check_all(session: Session) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    skus = {p.id: p.sku for p in session.scalars(select(Product)).all()}

    # 1. Двойная запись: сумма всех проводок по артикулу равна нулю.
    rows = session.execute(
        select(LedgerEntry.product_id, func.sum(LedgerEntry.qty_delta).label("total"))
        .group_by(LedgerEntry.product_id)
        .having(func.sum(LedgerEntry.qty_delta) != 0)
    ).all()
    checks.append(
        _check(
            "ledger_balanced",
            "Сумма проводок по каждому артикулу = 0 (товар не исчезает и не появляется)",
            [{"sku": skus.get(pid, pid), "sum": int(total)} for pid, total in rows],
        )
    )

    # 2. Остатки — точная проекция журнала.
    bal = select(
        StockBalance.product_id, StockBalance.location_id, StockBalance.bucket, StockBalance.qty
    ).subquery()
    led = (
        select(
            LedgerEntry.product_id,
            LedgerEntry.location_id,
            LedgerEntry.bucket,
            func.sum(LedgerEntry.qty_delta).label("qty"),
        )
        .group_by(LedgerEntry.product_id, LedgerEntry.location_id, LedgerEntry.bucket)
        .subquery()
    )
    join_cond = and_(
        bal.c.product_id == led.c.product_id,
        bal.c.location_id == led.c.location_id,
        bal.c.bucket == led.c.bucket,
    )
    rows = session.execute(
        select(
            func.coalesce(bal.c.product_id, led.c.product_id),
            func.coalesce(bal.c.location_id, led.c.location_id),
            func.coalesce(bal.c.bucket, led.c.bucket),
            bal.c.qty,
            led.c.qty,
        )
        .select_from(bal.outerjoin(led, join_cond, full=True))
        .where(or_(bal.c.qty.is_(None), led.c.qty.is_(None), bal.c.qty != led.c.qty))
    ).all()
    checks.append(
        _check(
            "projection_matches_ledger",
            "Остатки в точности равны сумме проводок журнала",
            [
                {
                    "sku": skus.get(pid, pid),
                    "location_id": lid,
                    "bucket": bucket,
                    "balance": b_qty,
                    "ledger": l_qty,
                }
                for pid, lid, bucket, b_qty, l_qty in rows
            ],
        )
    )

    # 3. Нет отрицательных остатков там, где их быть не может.
    rows = session.execute(
        select(Product.sku, Location.name, StockBalance.bucket, StockBalance.qty)
        .join(Location, Location.id == StockBalance.location_id)
        .join(Product, Product.id == StockBalance.product_id)
        .where(Location.allow_negative.is_(False), StockBalance.qty < 0)
    ).all()
    checks.append(
        _check(
            "no_negative_stock",
            "Ни один склад/узел (кроме поставщика) не уходит в минус",
            [
                {"sku": sku, "location": name, "bucket": bucket, "qty": qty}
                for sku, name, bucket, qty in rows
            ],
        )
    )

    # 4. Товар «в пути» совпадает с содержимым отправленных документов.
    transit_actual: dict[int, int] = defaultdict(int)
    for lid, qty in session.execute(
        select(StockBalance.location_id, StockBalance.qty)
        .join(Location, Location.id == StockBalance.location_id)
        .where(Location.kind == LocationKind.TRANSIT.value)
    ).all():
        transit_actual[lid] += qty

    transit_expected: dict[int, int] = {}
    transit_docs = session.execute(
        select(Location.id, Location.document_id, Document.status, Document.kind).join(
            Document, Document.id == Location.document_id
        )
    ).all()
    line_sums = dict(
        session.execute(
            select(DocumentLine.document_id, func.sum(DocumentLine.qty)).group_by(
                DocumentLine.document_id
            )
        ).all()
    )
    for lid, doc_id, status, _kind in transit_docs:
        transit_expected[lid] = (
            int(line_sums.get(doc_id, 0)) if status == DocStatus.SHIPPED.value else 0
        )
    violations = [
        {"location_id": lid, "in_transit": transit_actual.get(lid, 0), "expected": expected}
        for lid, expected in transit_expected.items()
        if transit_actual.get(lid, 0) != expected
    ]
    checks.append(
        _check(
            "transit_matches_documents",
            "«В пути» числится ровно то, что отправлено и ещё не принято",
            violations,
        )
    )

    # 5. Резерв FBS совпадает с суммой активных (неотправленных) заказов.
    reserved_actual: dict[tuple[int, int], int] = defaultdict(int)
    wh_location = {
        loc_id: wh_id
        for loc_id, wh_id in session.execute(
            select(Location.id, Location.warehouse_id).where(
                Location.kind == LocationKind.FULFILLMENT.value
            )
        ).all()
    }
    for pid, lid, qty in session.execute(
        select(StockBalance.product_id, StockBalance.location_id, StockBalance.qty).where(
            StockBalance.bucket == Bucket.RESERVED.value
        )
    ).all():
        if lid in wh_location:
            reserved_actual[(pid, wh_location[lid])] += qty

    reserved_expected: dict[tuple[int, int], int] = defaultdict(int)
    for pid, wh_id, qty in session.execute(
        select(DocumentLine.product_id, Document.from_warehouse_id, DocumentLine.qty)
        .join(Document, Document.id == DocumentLine.document_id)
        .where(
            Document.kind == DocKind.FBS.value,
            Document.status == DocStatus.RESERVED.value,
        )
    ).all():
        reserved_expected[(pid, wh_id)] += qty

    keys = set(reserved_actual) | set(reserved_expected)
    violations = [
        {
            "sku": skus.get(pid, pid),
            "warehouse_id": wh_id,
            "reserved": reserved_actual.get((pid, wh_id), 0),
            "expected": reserved_expected.get((pid, wh_id), 0),
        }
        for (pid, wh_id) in sorted(keys)
        if reserved_actual.get((pid, wh_id), 0) != reserved_expected.get((pid, wh_id), 0)
    ]
    checks.append(
        _check(
            "reserved_matches_fbs",
            "Резерв на ФФ равен сумме активных FBS-заказов",
            violations,
        )
    )

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "warehouses": session.scalar(select(func.count(Warehouse.id))) or 0,
    }

"""Модели чтения: экран остатков, «где числится товар», история операций.

Читаем из проекции остатков (stock_balances) с join'ом на узлы учёта, поэтому
все подписи строятся из актуальных данных: переименовал ФФ — имя обновилось
везде, включая товар в пути и историю.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    DocKind,
    Document,
    DocumentEvent,
    DocumentLine,
    LedgerEntry,
    Location,
    LocationKind,
    Product,
    StockBalance,
    Warehouse,
)
from app.serializers import (
    KIND_LABELS,
    STATUS_LABELS,
    document_payload,
    location_label,
    warehouse_ref,
)

TRANSIT_COLUMNS = {
    DocKind.TRANSFER.value: "between_ff",
    DocKind.FBO.value: "to_marketplace",
    DocKind.FBS.value: "to_client",
}


def _balance_rows(session: Session):
    return session.execute(
        select(StockBalance, Location, Warehouse, Document)
        .join(Location, Location.id == StockBalance.location_id)
        .outerjoin(Warehouse, Warehouse.id == Location.warehouse_id)
        .outerjoin(Document, Document.id == Location.document_id)
    ).all()


def stock_matrix(
    session: Session, include_archived: bool = False, search: str | None = None
) -> dict[str, Any]:
    products = session.scalars(select(Product).order_by(Product.sku)).all()
    if search:
        needle = search.strip().lower()
        products = [p for p in products if needle in p.sku.lower() or needle in p.name.lower()]

    warehouses = session.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    rows = _balance_rows(session)

    per_product: dict[int, dict[str, Any]] = {}
    for product in products:
        per_product[product.id] = {
            "ff": defaultdict(lambda: {"free": 0, "reserved": 0}),
            "transit": {"between_ff": 0, "to_marketplace": 0, "to_client": 0},
            "marketplace": 0,
            "delivered": 0,
            "received": 0,
        }

    for bal, loc, wh, doc in rows:
        agg = per_product.get(bal.product_id)
        if agg is None:
            continue
        if loc.kind == LocationKind.FULFILLMENT.value and wh is not None:
            agg["ff"][wh.id][bal.bucket] += bal.qty
        elif loc.kind == LocationKind.TRANSIT.value and doc is not None:
            column = TRANSIT_COLUMNS.get(doc.kind)
            if column:
                agg["transit"][column] += bal.qty
        elif loc.kind == LocationKind.MARKETPLACE.value:
            agg["marketplace"] += bal.qty
        elif loc.kind == LocationKind.CLIENT.value:
            agg["delivered"] += bal.qty
        elif loc.kind == LocationKind.SUPPLIER.value:
            agg["received"] += -bal.qty

    # Архивный ФФ показываем, если на нём что-то числится: товар нельзя «спрятать».
    visible = [
        w
        for w in warehouses
        if not w.is_archived
        or include_archived
        or any(
            agg["ff"].get(w.id, {"free": 0, "reserved": 0})["free"]
            or agg["ff"].get(w.id, {"free": 0, "reserved": 0})["reserved"]
            for agg in per_product.values()
        )
    ]

    result = []
    for product in products:
        agg = per_product[product.id]
        cells = []
        for wh in visible:
            data = agg["ff"].get(wh.id, {"free": 0, "reserved": 0})
            cells.append(
                {
                    "warehouse_id": wh.id,
                    "name": wh.name,
                    "archived": wh.is_archived,
                    "available": data["free"],
                    "reserved": data["reserved"],
                    "on_hand": data["free"] + data["reserved"],
                }
            )
        totals = {
            "available": sum(c["available"] for c in cells),
            "reserved": sum(c["reserved"] for c in cells),
            "on_hand": sum(c["on_hand"] for c in cells),
        }
        transit_total = sum(agg["transit"].values())
        result.append(
            {
                "product_id": product.id,
                "sku": product.sku,
                "name": product.name,
                "warehouses": cells,
                "totals": totals,
                "in_transit": dict(agg["transit"], total=transit_total),
                "marketplace": agg["marketplace"],
                "delivered": agg["delivered"],
                "received_total": agg["received"],
                "accounted_total": totals["on_hand"]
                + transit_total
                + agg["marketplace"]
                + agg["delivered"],
            }
        )

    return {
        "warehouses": [warehouse_ref(w) for w in visible],
        "rows": result,
        "grand_totals": {
            "on_hand": sum(r["totals"]["on_hand"] for r in result),
            "available": sum(r["totals"]["available"] for r in result),
            "reserved": sum(r["totals"]["reserved"] for r in result),
            "in_transit": sum(r["in_transit"]["total"] for r in result),
            "marketplace": sum(r["marketplace"] for r in result),
            "delivered": sum(r["delivered"] for r in result),
        },
    }


def where_is(session: Session, sku: str) -> dict[str, Any]:
    """Разложение по всем местам, где числится артикул (включая архивные ФФ)."""
    product = session.scalar(select(Product).where(Product.sku == sku))
    if product is None:
        from app.errors import NotFoundError

        raise NotFoundError(f"Артикул {sku} не найден")

    warehouses = {w.id: w for w in session.scalars(select(Warehouse)).all()}
    positions = []
    for bal, loc, wh, doc in _balance_rows(session):
        if bal.product_id != product.id or bal.qty == 0:
            continue
        positions.append(
            {
                "qty": bal.qty,
                "bucket": bal.bucket,
                "location_kind": loc.kind,
                "label": location_label(loc, bal.bucket, wh, doc, warehouses),
                "warehouse": warehouse_ref(wh),
                "document": (
                    {
                        "id": doc.id,
                        "kind": doc.kind,
                        "kind_label": KIND_LABELS.get(doc.kind, doc.kind),
                        "status": doc.status,
                        "status_label": STATUS_LABELS.get(doc.status, doc.status),
                        "reference": doc.reference,
                    }
                    if doc is not None
                    else None
                ),
            }
        )
    order = {
        LocationKind.FULFILLMENT.value: 0,
        LocationKind.TRANSIT.value: 1,
        LocationKind.MARKETPLACE.value: 2,
        LocationKind.CLIENT.value: 3,
        LocationKind.SUPPLIER.value: 4,
    }
    positions.sort(key=lambda p: (order.get(p["location_kind"], 9), p["label"]))
    return {
        "sku": product.sku,
        "name": product.name,
        "positions": positions,
        "total_accounted": sum(p["qty"] for p in positions),  # с учётом минуса поставщика = 0
    }


def list_documents(
    session: Session,
    kind: str | None = None,
    status: str | None = None,
    warehouse_id: int | None = None,
    sku: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    stmt = (
        select(Document)
        .options(selectinload(Document.lines).selectinload(DocumentLine.product))
        .order_by(Document.id.desc())
        .limit(limit)
    )
    if kind:
        stmt = stmt.where(Document.kind == kind)
    if status:
        stmt = stmt.where(Document.status == status)
    if warehouse_id:
        stmt = stmt.where(
            or_(
                Document.from_warehouse_id == warehouse_id,
                Document.to_warehouse_id == warehouse_id,
            )
        )
    if sku:
        stmt = stmt.where(
            Document.id.in_(
                select(DocumentLine.document_id)
                .join(Product, Product.id == DocumentLine.product_id)
                .where(Product.sku == sku)
            )
        )
    return [document_payload(session, doc) for doc in session.scalars(stmt).unique().all()]


def history(
    session: Session,
    limit: int = 100,
    kind: str | None = None,
    warehouse_id: int | None = None,
    sku: str | None = None,
) -> list[dict[str, Any]]:
    """Журнал: какой товар, сколько единиц, откуда и куда переместился."""
    stmt = (
        select(DocumentEvent, Document)
        .join(Document, Document.id == DocumentEvent.document_id)
        .order_by(DocumentEvent.id.desc())
        .limit(limit)
    )
    if kind:
        stmt = stmt.where(Document.kind == kind)
    if warehouse_id:
        stmt = stmt.where(
            or_(
                Document.from_warehouse_id == warehouse_id,
                Document.to_warehouse_id == warehouse_id,
            )
        )
    if sku:
        stmt = stmt.where(
            DocumentEvent.document_id.in_(
                select(DocumentLine.document_id)
                .join(Product, Product.id == DocumentLine.product_id)
                .where(Product.sku == sku)
            )
        )
    events = session.execute(stmt).all()
    if not events:
        return []

    warehouses = {w.id: w for w in session.scalars(select(Warehouse)).all()}
    event_ids = [e.id for e, _ in events]
    entries = session.execute(
        select(LedgerEntry, Location, Product, Warehouse, Document)
        .join(Location, Location.id == LedgerEntry.location_id)
        .join(Product, Product.id == LedgerEntry.product_id)
        .outerjoin(Warehouse, Warehouse.id == Location.warehouse_id)
        .outerjoin(Document, Document.id == Location.document_id)
        .where(LedgerEntry.event_id.in_(event_ids))
        .order_by(LedgerEntry.id)
    ).all()

    grouped: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    for entry, loc, product, wh, doc in entries:
        move = grouped[entry.event_id].setdefault(
            entry.product_id,
            {"sku": product.sku, "name": product.name, "qty": 0, "from": [], "to": []},
        )
        label = location_label(loc, entry.bucket, wh, doc, warehouses)
        if entry.qty_delta < 0:
            move["from"].append(label)
            move["qty"] += -entry.qty_delta
        else:
            move["to"].append(label)

    result = []
    for event, doc in events:
        moves = list(grouped.get(event.id, {}).values())
        result.append(
            {
                "event_id": event.id,
                "at": event.created_at.isoformat() if event.created_at else None,
                "document_id": doc.id,
                "kind": doc.kind,
                "kind_label": KIND_LABELS.get(doc.kind, doc.kind),
                "reference": doc.reference,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "to_status_label": STATUS_LABELS.get(event.to_status, event.to_status),
                "idempotency_key": event.idempotency_key,
                "moves": [
                    {
                        "sku": m["sku"],
                        "name": m["name"],
                        "qty": m["qty"],
                        "from": " + ".join(m["from"]) or "—",
                        "to": " + ".join(m["to"]) or "—",
                    }
                    for m in moves
                ],
            }
        )
    return result

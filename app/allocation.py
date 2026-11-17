"""Нехватка на выбранном ФФ: раскладка и варианты решения.

Требование ТЗ: «Обработайте нехватку на выбранном ФФ при достаточном общем
остатке». Мы не просто отказываем, а объясняем ситуацию и предлагаем действия:
переместить недостающее с другого ФФ или разбить отгрузку между складами.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import InsufficientStock
from app.models import Bucket, Location, LocationKind, Product, StockBalance, Warehouse


def available_by_warehouse(session: Session, product_id: int) -> dict[int, int]:
    """Доступный (не зарезервированный) остаток артикула по всем ФФ, включая архивные."""
    rows = session.execute(
        select(Location.warehouse_id, StockBalance.qty)
        .join(StockBalance, StockBalance.location_id == Location.id)
        .where(
            Location.kind == LocationKind.FULFILLMENT.value,
            StockBalance.product_id == product_id,
            StockBalance.bucket == Bucket.FREE.value,
        )
    ).all()
    return {wh_id: qty for wh_id, qty in rows if wh_id is not None}


def _active_warehouses(session: Session) -> dict[int, Warehouse]:
    rows = session.scalars(
        select(Warehouse).where(Warehouse.archived_at.is_(None)).order_by(Warehouse.id)
    ).all()
    return {w.id: w for w in rows}


def analyze_shortage(
    session: Session, product: Product, warehouse_id: int, requested: int
) -> dict[str, Any]:
    """Полная раскладка по нехватке: сколько где есть и что можно сделать."""
    available = available_by_warehouse(session, product.id)
    active = _active_warehouses(session)
    here = available.get(warehouse_id, 0)
    total = sum(qty for wh_id, qty in available.items() if wh_id in active)

    others = sorted(
        (
            {"warehouse_id": wh_id, "name": active[wh_id].name, "available": qty}
            for wh_id, qty in available.items()
            if wh_id != warehouse_id and wh_id in active and qty > 0
        ),
        key=lambda row: (-row["available"], row["warehouse_id"]),
    )

    target = session.get(Warehouse, warehouse_id)
    shortfall = max(requested - here, 0)
    transfer_plan: list[dict[str, Any]] = []
    left = shortfall
    for row in others:
        if left <= 0:
            break
        take = min(left, row["available"])
        transfer_plan.append(
            {
                "from_warehouse_id": row["warehouse_id"],
                "from_name": row["name"],
                "to_warehouse_id": warehouse_id,
                "to_name": target.name if target else str(warehouse_id),
                "qty": take,
            }
        )
        left -= take

    split_plan = (
        allocate(session, product.id, warehouse_id, requested) if total >= requested else []
    )

    return {
        "product_id": product.id,
        "sku": product.sku,
        "product_name": product.name,
        "warehouse_id": warehouse_id,
        "requested": requested,
        "available_on_warehouse": here,
        "shortfall": shortfall,
        "total_available": total,
        "enough_in_total": total >= requested,
        "other_warehouses": others,
        "transfer_plan": transfer_plan if left == 0 else [],
        "split_plan": [
            {"warehouse_id": wh_id, "name": active[wh_id].name, "qty": qty}
            for wh_id, qty in split_plan
            if wh_id in active
        ],
    }


def ensure_available(session: Session, warehouse_id: int, lines: list[tuple[Product, int]]) -> None:
    """Проверка перед резервом/отгрузкой. Бросает InsufficientStock с раскладкой."""
    for product, qty in lines:
        available = available_by_warehouse(session, product.id).get(warehouse_id, 0)
        if available < qty:
            details = analyze_shortage(session, product, warehouse_id, qty)
            wh = session.get(Warehouse, warehouse_id)
            hint = ""
            if details["enough_in_total"]:
                hint = (
                    f" Всего по сети доступно {details['total_available']} шт — "
                    "можно переместить недостающее с другого ФФ или разбить отгрузку."
                )
            raise InsufficientStock(
                f"На складе «{wh.name if wh else warehouse_id}» доступно "
                f"{available} шт {product.sku}, нужно {qty}.{hint}",
                details,
            )


def allocate(
    session: Session, product_id: int, preferred_warehouse_id: int, qty: int
) -> list[tuple[int, int]]:
    """Разложить количество по складам: сначала выбранный ФФ, потом самые полные.

    Возвращает [] если суммарно товара не хватает.
    """
    available = available_by_warehouse(session, product_id)
    active = _active_warehouses(session)
    order = sorted(
        (wh_id for wh_id in active if available.get(wh_id, 0) > 0),
        key=lambda wh_id: (wh_id != preferred_warehouse_id, -available.get(wh_id, 0), wh_id),
    )
    plan: list[tuple[int, int]] = []
    left = qty
    for wh_id in order:
        if left <= 0:
            break
        take = min(left, available[wh_id])
        plan.append((wh_id, take))
        left -= take
    return plan if left == 0 else []

"""Справочники: магазин, склады фулфилмента (ФФ), артикулы.

Ключевое решение: удаление ФФ — это архивирование (soft delete). Физически
строку не удаляем, потому что вместе с ней исчезли бы остатки и история,
а ТЗ требует их сохранить. Архивный ФФ закрыт для новых документов, но
начатые документы можно довести до конца — иначе товар зависнет в пути.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError, ValidationFailed
from app.locations import ensure_singletons, warehouse_location
from app.models import (
    Bucket,
    Location,
    LocationKind,
    Product,
    Shop,
    StockBalance,
    Warehouse,
)

DEFAULT_SHOP_NAME = "Магазин Ракета"


def default_shop(session: Session) -> Shop:
    shop = session.scalar(select(Shop).order_by(Shop.id).limit(1))
    if shop is None:
        shop = Shop(name=DEFAULT_SHOP_NAME)
        session.add(shop)
        session.flush()
        ensure_singletons(session)
    return shop


def warehouse_units(session: Session, warehouse_id: int) -> int:
    """Сколько единиц физически числится на ФФ (доступно + резерв)."""
    loc = session.scalar(select(Location).where(Location.warehouse_id == warehouse_id))
    if loc is None:
        return 0
    total = session.scalar(
        select(func.coalesce(func.sum(StockBalance.qty), 0)).where(
            StockBalance.location_id == loc.id
        )
    )
    return int(total or 0)


def warehouse_payload(session: Session, wh: Warehouse) -> dict[str, Any]:
    return {
        "id": wh.id,
        "name": wh.name,
        "shop_id": wh.shop_id,
        "archived": wh.is_archived,
        "archived_at": wh.archived_at.isoformat() if wh.archived_at else None,
        "units_on_hand": warehouse_units(session, wh.id),
    }


def list_warehouses(session: Session, include_archived: bool = True) -> list[dict[str, Any]]:
    stmt = select(Warehouse).order_by(Warehouse.archived_at.nulls_first(), Warehouse.id)
    if not include_archived:
        stmt = stmt.where(Warehouse.archived_at.is_(None))
    return [warehouse_payload(session, wh) for wh in session.scalars(stmt).all()]


def _check_name_free(session: Session, shop_id: int, name: str, exclude_id: int | None) -> None:
    stmt = select(Warehouse).where(
        Warehouse.shop_id == shop_id,
        func.lower(Warehouse.name) == name.lower(),
        Warehouse.archived_at.is_(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(Warehouse.id != exclude_id)
    if session.scalar(stmt) is not None:
        raise ConflictError(f"Активный склад с именем «{name}» уже есть", {"name": name})


def create_warehouse(session: Session, name: str, shop_id: int | None = None) -> Warehouse:
    name = (name or "").strip()
    if not name:
        raise ValidationFailed("Имя склада не может быть пустым")
    shop = session.get(Shop, shop_id) if shop_id else default_shop(session)
    if shop is None:
        raise NotFoundError(f"Магазин #{shop_id} не найден")
    _check_name_free(session, shop.id, name, None)
    wh = Warehouse(shop_id=shop.id, name=name)
    session.add(wh)
    session.flush()
    warehouse_location(session, wh.id)  # узел учёта создаём сразу
    return wh


def get_warehouse(session: Session, warehouse_id: int) -> Warehouse:
    wh = session.get(Warehouse, warehouse_id)
    if wh is None:
        raise NotFoundError(f"Склад #{warehouse_id} не найден")
    return wh


def rename_warehouse(session: Session, warehouse_id: int, name: str) -> Warehouse:
    name = (name or "").strip()
    if not name:
        raise ValidationFailed("Имя склада не может быть пустым")
    wh = get_warehouse(session, warehouse_id)
    _check_name_free(session, wh.shop_id, name, wh.id)
    wh.name = name
    loc = session.scalar(select(Location).where(Location.warehouse_id == wh.id))
    if loc is not None:
        loc.name = name
    session.flush()
    return wh


def archive_warehouse(session: Session, warehouse_id: int) -> dict[str, Any]:
    """«Удалить» ФФ: архивируем, остатки и история остаются на месте."""
    wh = get_warehouse(session, warehouse_id)
    if not wh.is_archived:
        wh.archived_at = datetime.now(UTC)
        session.flush()
    payload = warehouse_payload(session, wh)
    payload["message"] = (
        f"Склад «{wh.name}» отправлен в архив. На нём числится {payload['units_on_hand']} ед. — "
        "остатки и история сохранены, новые операции запрещены."
    )
    return payload


def restore_warehouse(session: Session, warehouse_id: int) -> Warehouse:
    wh = get_warehouse(session, warehouse_id)
    if wh.is_archived:
        _check_name_free(session, wh.shop_id, wh.name, wh.id)
        wh.archived_at = None
        session.flush()
    return wh


def warehouse_stock_detail(session: Session, warehouse_id: int) -> list[dict[str, Any]]:
    """Что именно лежит на складе (для подтверждения архивирования)."""
    loc = session.scalar(select(Location).where(Location.warehouse_id == warehouse_id))
    if loc is None:
        return []
    rows = session.execute(
        select(Product.sku, Product.name, StockBalance.bucket, StockBalance.qty)
        .join(Product, Product.id == StockBalance.product_id)
        .where(StockBalance.location_id == loc.id, StockBalance.qty != 0)
        .order_by(Product.sku, StockBalance.bucket)
    ).all()
    return [
        {"sku": sku, "name": name, "bucket": bucket, "qty": qty} for sku, name, bucket, qty in rows
    ]


def list_products(session: Session) -> list[dict[str, Any]]:
    return [
        {"id": p.id, "sku": p.sku, "name": p.name}
        for p in session.scalars(select(Product).order_by(Product.sku)).all()
    ]


def create_product(session: Session, sku: str, name: str) -> Product:
    sku, name = (sku or "").strip(), (name or "").strip()
    if not sku or not name:
        raise ValidationFailed("Нужны и артикул, и название")
    if session.scalar(select(Product).where(Product.sku == sku)) is not None:
        raise ConflictError(f"Артикул {sku} уже существует", {"sku": sku})
    product = Product(sku=sku, name=name)
    session.add(product)
    session.flush()
    return product


def reserved_units(session: Session, warehouse_id: int) -> int:
    loc = session.scalar(
        select(Location).where(
            Location.warehouse_id == warehouse_id,
            Location.kind == LocationKind.FULFILLMENT.value,
        )
    )
    if loc is None:
        return 0
    total = session.scalar(
        select(func.coalesce(func.sum(StockBalance.qty), 0)).where(
            StockBalance.location_id == loc.id, StockBalance.bucket == Bucket.RESERVED.value
        )
    )
    return int(total or 0)

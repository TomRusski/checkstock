"""Узлы учёта (locations): где числится товар.

Узлы создаются лениво и один раз: у каждого ФФ свой узел, у каждого документа
в пути — свой, поставщик/маркетплейс/клиент — общие singleton-узлы.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFoundError
from app.models import Document, Location, LocationKind, Warehouse

SINGLETON_NAMES = {
    LocationKind.SUPPLIER: "Поставщик",
    LocationKind.MARKETPLACE: "Склад маркетплейса",
    LocationKind.CLIENT: "Клиенты (доставлено)",
}


def singleton_location(session: Session, kind: LocationKind) -> Location:
    if kind not in SINGLETON_NAMES:
        raise ValueError(f"{kind} не singleton-узел")
    loc = session.scalar(select(Location).where(Location.kind == kind.value))
    if loc is None:
        loc = Location(
            kind=kind.value,
            name=SINGLETON_NAMES[kind],
            allow_negative=(kind is LocationKind.SUPPLIER),
        )
        session.add(loc)
        session.flush()
    return loc


def warehouse_location(session: Session, warehouse_id: int) -> Location:
    loc = session.scalar(select(Location).where(Location.warehouse_id == warehouse_id))
    if loc is None:
        wh = session.get(Warehouse, warehouse_id)
        if wh is None:
            raise NotFoundError(f"Склад #{warehouse_id} не найден")
        loc = Location(kind=LocationKind.FULFILLMENT.value, name=wh.name, warehouse_id=wh.id)
        session.add(loc)
        session.flush()
    return loc


def transit_location(session: Session, document: Document) -> Location:
    """Узел «в пути» для документа. Один документ — один узел, поэтому
    товар в пути всегда можно разложить по конкретным отправлениям."""
    loc = session.scalar(select(Location).where(Location.document_id == document.id))
    if loc is None:
        loc = Location(
            kind=LocationKind.TRANSIT.value,
            name=f"В пути по документу #{document.id}",
            document_id=document.id,
        )
        session.add(loc)
        session.flush()
    return loc


def ensure_singletons(session: Session) -> None:
    for kind in SINGLETON_NAMES:
        singleton_location(session, kind)

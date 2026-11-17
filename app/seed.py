"""Тестовые данные ровно из ТЗ: один магазин, два ФФ, три артикула.

    Артикул   Товар        Север  Юг
    SKU-101   Органайзер     10    6
    SKU-102   Контейнер       5    0
    SKU-103   Корзина         0    4

Остатки создаются не «UPDATE поля», а проведёнными документами прихода —
поэтому история операций с самого начала непустая и остатки сходятся.

Запуск:  python -m app.seed --force   (полный сброс)
         python -m app.seed --if-empty
"""

from __future__ import annotations

import argparse
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.catalog import create_product, create_warehouse, default_shop
from app.documents import create_document, transition
from app.locations import ensure_singletons
from app.models import DocKind, DocStatus, Product
from app.serializers import document_payload

TEST_DATA = [
    ("SKU-101", "Органайзер", {"Север": 10, "Юг": 6}),
    ("SKU-102", "Контейнер", {"Север": 5, "Юг": 0}),
    ("SKU-103", "Корзина", {"Север": 0, "Юг": 4}),
]

TRUNCATE_SQL = text(
    "TRUNCATE TABLE ledger_entries, stock_balances, document_events, document_lines, "
    "documents, locations, products, warehouses, shops RESTART IDENTITY CASCADE"
)


def reset_demo_data(session: Session) -> dict[str, Any]:
    """Полная очистка и засев тестовых данных. Используется кнопкой в UI и тестами."""
    session.execute(TRUNCATE_SQL)
    session.flush()

    shop = default_shop(session)
    ensure_singletons(session)
    warehouses = {name: create_warehouse(session, name, shop.id) for name in ("Север", "Юг")}
    products = {sku: create_product(session, sku, name) for sku, name, _ in TEST_DATA}

    receipts = []
    for wh_name, wh in warehouses.items():
        lines = [
            (products[sku], qty[wh_name])
            for sku, _name, qty in TEST_DATA
            if qty.get(wh_name, 0) > 0
        ]
        if not lines:
            continue
        doc, _event = create_document(
            session,
            kind=DocKind.RECEIPT.value,
            shop_id=shop.id,
            lines=lines,
            to_warehouse_id=wh.id,
            reference=f"Тестовый приход · {wh_name}",
            note="Тестовые данные из ТЗ",
        )
        transition(session, doc.id, DocStatus.POSTED.value, expected_kind=DocKind.RECEIPT.value)
        receipts.append(document_payload(session, doc))

    session.flush()
    return {
        "shop": {"id": shop.id, "name": shop.name},
        "warehouses": [{"id": w.id, "name": w.name} for w in warehouses.values()],
        "products": [{"id": p.id, "sku": p.sku, "name": p.name} for p in products.values()],
        "receipts": receipts,
    }


def seed_if_empty(session: Session) -> bool:
    if session.scalar(select(Product.id).limit(1)) is not None:
        return False
    reset_demo_data(session)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Тестовые данные CheckStock")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--force", action="store_true", help="очистить базу и засеять заново")
    group.add_argument("--if-empty", action="store_true", help="сеять только если база пустая")
    args = parser.parse_args()

    from app.db import SessionLocal

    with SessionLocal() as session:
        if args.force:
            data = reset_demo_data(session)
            print(f"База пересоздана: {len(data['products'])} артикула, 2 ФФ, приход проведён")
        else:
            created = seed_if_empty(session)
            print("Тестовые данные созданы" if created else "База не пустая — данные не тронуты")
        session.commit()


if __name__ == "__main__":
    main()

"""Учётное ядро: проводки двойной записи и проекция остатков.

Правила, которые здесь защищены:
1. Сумма проводок по каждому артикулу внутри события = 0 (товар не появляется
   из ниоткуда и не исчезает: он переезжает между узлами/карманами).
2. Баланс узла не может уйти в минус, если узел не помечен allow_negative
   (исключение — «Поставщик», бесконечный источник прихода).
3. Строки остатков блокируются SELECT ... FOR UPDATE в детерминированном
   порядке — параллельные подтверждения не пробивают остаток в минус и не
   устраивают deadlock.
4. Журнал append-only: проводки только добавляются (плюс триггер в БД).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.errors import AccountingError, InsufficientStock
from app.models import Bucket, LedgerEntry, Location, StockBalance


@dataclass(frozen=True, slots=True)
class Posting:
    product_id: int
    location_id: int
    bucket: Bucket
    delta: int

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.product_id, self.location_id, self.bucket.value)


def apply_postings(session: Session, event_id: int, postings: list[Posting]) -> None:
    """Применить сбалансированный набор проводок к журналу и остаткам."""
    if not postings:
        return

    per_product: dict[int, int] = defaultdict(int)
    for p in postings:
        if p.delta == 0:
            raise AccountingError("Проводка с нулевым количеством")
        per_product[p.product_id] += p.delta
    unbalanced = {pid: total for pid, total in per_product.items() if total != 0}
    if unbalanced:
        raise AccountingError("Проводки не сбалансированы по артикулам", {"unbalanced": unbalanced})

    # Детерминированный порядок блокировок: сначала по артикулу, потом по узлу.
    ordered = sorted(postings, key=lambda p: p.key)

    # 1. Гарантируем, что строки остатков существуют (иначе блокировать нечего).
    for p in ordered:
        session.execute(
            pg_insert(StockBalance)
            .values(
                product_id=p.product_id, location_id=p.location_id, bucket=p.bucket.value, qty=0
            )
            .on_conflict_do_nothing()
        )

    # 2. Блокируем строки и проверяем достаточность.
    negative_locations = set(
        session.scalars(select(Location.id).where(Location.allow_negative.is_(True))).all()
    )
    current: dict[tuple[int, int, str], int] = {}
    for p in ordered:
        row = session.execute(
            select(StockBalance)
            .where(
                StockBalance.product_id == p.product_id,
                StockBalance.location_id == p.location_id,
                StockBalance.bucket == p.bucket.value,
            )
            .with_for_update()
        ).scalar_one()
        current[p.key] = row.qty

    running = dict(current)
    for p in ordered:
        running[p.key] += p.delta
        if running[p.key] < 0 and p.location_id not in negative_locations:
            raise InsufficientStock(
                "Недостаточно товара на складе для этой операции",
                {
                    "product_id": p.product_id,
                    "location_id": p.location_id,
                    "bucket": p.bucket.value,
                    "requested": -p.delta,
                    "available": current[p.key],
                },
            )

    # 3. Пишем журнал и обновляем проекцию.
    for p in ordered:
        session.add(
            LedgerEntry(
                event_id=event_id,
                product_id=p.product_id,
                location_id=p.location_id,
                bucket=p.bucket.value,
                qty_delta=p.delta,
            )
        )
        balance = session.get(StockBalance, p.key)
        assert balance is not None  # строка заблокирована выше
        balance.qty = running[p.key]
    session.flush()


def rebuild_projection(session: Session) -> int:
    """Пересобрать остатки с нуля из журнала. Возвращает число строк.

    Используется тестами (доказывает, что остатки — именно проекция журнала)
    и может использоваться как аварийная процедура восстановления.
    """
    rows = session.execute(
        select(
            LedgerEntry.product_id,
            LedgerEntry.location_id,
            LedgerEntry.bucket,
            LedgerEntry.qty_delta,
        )
    ).all()
    totals: dict[tuple[int, int, str], int] = defaultdict(int)
    for product_id, location_id, bucket, delta in rows:
        totals[(product_id, location_id, bucket)] += delta

    session.query(StockBalance).delete()
    for (product_id, location_id, bucket), qty in totals.items():
        session.add(
            StockBalance(product_id=product_id, location_id=location_id, bucket=bucket, qty=qty)
        )
    session.flush()
    return len(totals)

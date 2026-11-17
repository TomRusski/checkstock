"""Схема данных CheckStock.

Идея модели: товар всегда «числится» на одном из УЗЛОВ (locations):
поставщик, склад фулфилмента, «в пути» по конкретному документу, склад
маркетплейса, клиент. Любая операция — это набор проводок между узлами,
сумма которых по каждому артикулу равна нулю (двойная запись).

Остатки (stock_balances) — материализованная проекция журнала (ledger_entries).
Её можно пересобрать из журнала и сравнить: это делает тест test_invariants.py.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class LocationKind(StrEnum):
    SUPPLIER = "supplier"  # источник прихода (баланс уходит в минус — это нормально)
    FULFILLMENT = "fulfillment"  # склад фулфилмента (ФФ)
    TRANSIT = "transit"  # «в пути» по конкретному документу
    MARKETPLACE = "marketplace"  # склад маркетплейса после приёмки FBO
    CLIENT = "client"  # доставлено клиенту по FBS


class Bucket(StrEnum):
    """Карман внутри узла. На ФФ товар физически лежит в двух карманах."""

    FREE = "free"  # доступно к продаже/отгрузке
    RESERVED = "reserved"  # зарезервировано под FBS-заказ


class DocKind(StrEnum):
    RECEIPT = "receipt"  # приход от поставщика на ФФ
    TRANSFER = "transfer"  # перемещение между ФФ
    FBS = "fbs"  # заказ клиента: резерв -> отправка -> доставка
    FBO = "fbo"  # поставка на склад маркетплейса


class DocStatus(StrEnum):
    DRAFT = "draft"
    POSTED = "posted"  # приход проведён
    RESERVED = "reserved"  # FBS: товар заблокирован на ФФ
    SHIPPED = "shipped"  # товар в пути
    RECEIVED = "received"  # принят на ФФ / на складе маркетплейса
    DELIVERED = "delivered"  # вручён клиенту
    CANCELLED = "cancelled"


LOCATION_KINDS = [k.value for k in LocationKind]
BUCKETS = [b.value for b in Bucket]
DOC_KINDS = [k.value for k in DocKind]
DOC_STATUSES = [s.value for s in DocStatus]
SINGLETON_KINDS = (LocationKind.SUPPLIER, LocationKind.MARKETPLACE, LocationKind.CLIENT)


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Warehouse(Base):
    """Склад фулфилмента. Удаление — только архивирование (soft delete)."""

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Имя уникально среди активных складов магазина. Архивные имена не мешают.
        Index(
            "uq_warehouse_active_name",
            "shop_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Location(Base):
    """Узел учёта: где физически или юридически числится товар."""

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id"), nullable=True, unique=True
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True, unique=True
    )
    # Отрицательный баланс разрешён только поставщику (бесконечный источник).
    allow_negative: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"kind IN {tuple(LOCATION_KINDS)!r}", name="ck_location_kind"),
        # Поставщик / маркетплейс / клиент — по одному узлу на систему.
        Index(
            "uq_location_singleton",
            "kind",
            unique=True,
            postgresql_where=text("kind IN ('supplier','marketplace','client')"),
        ),
    )


class Document(Base):
    """Документ движения: приход, перемещение, FBS-заказ, FBO-поставка."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    from_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id"), nullable=True
    )
    to_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lines: Mapped[list[DocumentLine]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="DocumentLine.id"
    )
    events: Mapped[list[DocumentEvent]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="DocumentEvent.id"
    )

    __table_args__ = (
        CheckConstraint(f"kind IN {tuple(DOC_KINDS)!r}", name="ck_document_kind"),
        CheckConstraint(f"status IN {tuple(DOC_STATUSES)!r}", name="ck_document_status"),
    )


class DocumentLine(Base):
    __tablename__ = "document_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    qty: Mapped[int] = mapped_column(Integer)

    document: Mapped[Document] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_line_qty_positive"),
        UniqueConstraint("document_id", "product_id", name="uq_line_document_product"),
    )


class DocumentEvent(Base):
    """Факт перехода документа между статусами. Основание для проводок.

    idempotency_key уникален: повторный запрос с тем же ключом не создаёт
    второе событие, а возвращает сохранённый снимок ответа.
    """

    __tablename__ = "document_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    response_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="events")


class LedgerEntry(Base):
    """Проводка. Только INSERT: таблица защищена триггером от UPDATE/DELETE."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("document_events.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), index=True)
    bucket: Mapped[str] = mapped_column(String(16))
    qty_delta: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"bucket IN {tuple(BUCKETS)!r}", name="ck_ledger_bucket"),
        CheckConstraint("qty_delta <> 0", name="ck_ledger_delta_nonzero"),
    )


class StockBalance(Base):
    """Остаток: проекция журнала по (артикул, узел, карман)."""

    __tablename__ = "stock_balances"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16), primary_key=True)
    qty: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (CheckConstraint(f"bucket IN {tuple(BUCKETS)!r}", name="ck_balance_bucket"),)

"""initial schema + guarantees at the database level

Revision ID: 0001
Revises:
Create Date: 2026-11-17

Схема сгенерирована alembic --autogenerate по app/models.py и причёсана руками.
В конце upgrade() добавлены два триггера — это страховка учёта на уровне БД:
  * ledger_entries нельзя изменить или удалить (журнал только дополняется);
  * stock_balances не может уйти в минус, кроме узла «Поставщик».
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku"),
    )
    op.create_table(
        "shops",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "warehouses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_warehouses_shop_id"), "warehouses", ["shop_id"], unique=False)
    # Уникальность имени только среди активных складов: архив не блокирует имя.
    op.create_index(
        "uq_warehouse_active_name",
        "warehouses",
        ["shop_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("from_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("to_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("reference", sa.String(length=120), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("kind IN ('receipt', 'transfer', 'fbs', 'fbo')", name="ck_document_kind"),
        sa.CheckConstraint(
            "status IN ('draft', 'posted', 'reserved', 'shipped', 'received', "
            "'delivered', 'cancelled')",
            name="ck_document_status",
        ),
        sa.ForeignKeyConstraint(["from_warehouse_id"], ["warehouses.id"]),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"]),
        sa.ForeignKeyConstraint(["to_warehouse_id"], ["warehouses.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_documents_kind"), "documents", ["kind"], unique=False)
    op.create_index(op.f("ix_documents_status"), "documents", ["status"], unique=False)
    op.create_table(
        "document_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("response_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        op.f("ix_document_events_document_id"), "document_events", ["document_id"], unique=False
    )
    op.create_table(
        "document_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.CheckConstraint("qty > 0", name="ck_line_qty_positive"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "product_id", name="uq_line_document_product"),
    )
    op.create_index(
        op.f("ix_document_lines_document_id"), "document_lines", ["document_id"], unique=False
    )
    op.create_index(
        op.f("ix_document_lines_product_id"), "document_lines", ["product_id"], unique=False
    )
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column(
            "allow_negative", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('supplier', 'fulfillment', 'transit', 'marketplace', 'client')",
            name="ck_location_kind",
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id"),
        sa.UniqueConstraint("warehouse_id"),
    )
    op.create_index(
        "uq_location_singleton",
        "locations",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("kind IN ('supplier','marketplace','client')"),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column("qty_delta", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("bucket IN ('free', 'reserved')", name="ck_ledger_bucket"),
        sa.CheckConstraint("qty_delta <> 0", name="ck_ledger_delta_nonzero"),
        sa.ForeignKeyConstraint(["event_id"], ["document_events.id"]),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ledger_entries_event_id"), "ledger_entries", ["event_id"], unique=False)
    op.create_index(
        op.f("ix_ledger_entries_location_id"), "ledger_entries", ["location_id"], unique=False
    )
    op.create_index(
        op.f("ix_ledger_entries_product_id"), "ledger_entries", ["product_id"], unique=False
    )
    op.create_table(
        "stock_balances",
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column("qty", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("bucket IN ('free', 'reserved')", name="ck_balance_bucket"),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("product_id", "location_id", "bucket"),
    )

    # --- Гарантии на уровне БД (не только в коде приложения) ---
    op.execute(
        """
        CREATE FUNCTION ledger_entries_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ledger_entries is append-only: % is blocked', TG_OP
                USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_append_only
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION ledger_entries_append_only();
        """
    )
    op.execute(
        """
        CREATE FUNCTION stock_balances_no_negative() RETURNS trigger AS $$
        DECLARE
            may_be_negative boolean;
        BEGIN
            IF NEW.qty >= 0 THEN
                RETURN NEW;
            END IF;
            SELECT allow_negative INTO may_be_negative FROM locations WHERE id = NEW.location_id;
            IF COALESCE(may_be_negative, false) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'negative stock blocked: product=% location=% bucket=% qty=%',
                NEW.product_id, NEW.location_id, NEW.bucket, NEW.qty
                USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_stock_balances_no_negative
        BEFORE INSERT OR UPDATE ON stock_balances
        FOR EACH ROW EXECUTE FUNCTION stock_balances_no_negative();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_stock_balances_no_negative ON stock_balances")
    op.execute("DROP FUNCTION IF EXISTS stock_balances_no_negative()")
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_append_only ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS ledger_entries_append_only()")
    op.drop_table("stock_balances")
    op.drop_table("ledger_entries")
    op.drop_index(
        "uq_location_singleton",
        table_name="locations",
        postgresql_where=sa.text("kind IN ('supplier','marketplace','client')"),
    )
    op.drop_table("locations")
    op.drop_table("document_lines")
    op.drop_table("document_events")
    op.drop_table("documents")
    op.drop_index(
        "uq_warehouse_active_name",
        table_name="warehouses",
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.drop_table("warehouses")
    op.drop_table("shops")
    op.drop_table("products")

"""Входные схемы API (Pydantic v2). Ответы — словари из моделей чтения."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LineIn(BaseModel):
    sku: str | None = Field(default=None, examples=["SKU-101"])
    product_id: int | None = None
    qty: int = Field(gt=0, le=1_000_000)


class ReceiptIn(BaseModel):
    to_warehouse_id: int
    lines: list[LineIn] = Field(min_length=1)
    reference: str | None = None
    note: str | None = None


class TransferIn(BaseModel):
    from_warehouse_id: int
    to_warehouse_id: int
    lines: list[LineIn] = Field(min_length=1)
    reference: str | None = None
    note: str | None = None


class FbsIn(BaseModel):
    from_warehouse_id: int
    lines: list[LineIn] = Field(min_length=1)
    reference: str | None = Field(default=None, examples=["WB-1001"])
    note: str | None = None
    allow_split: bool = False


class FboIn(BaseModel):
    from_warehouse_id: int
    lines: list[LineIn] = Field(min_length=1)
    reference: str | None = None
    note: str | None = None


class WarehouseIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    shop_id: int | None = None


class WarehouseRename(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProductIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)

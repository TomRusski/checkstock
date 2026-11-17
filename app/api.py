"""HTTP API CheckStock.

Соглашения:
* ошибки — {"error": {"code", "message", "details"}} с осмысленным HTTP-кодом;
* нехватка — 409 INSUFFICIENT_STOCK с раскладкой по складам и планом действий;
* повторное подтверждение — 409 CONFLICT (или 200 с тем же телом, если передан
  заголовок Idempotency-Key);
* все мутации принимают заголовок Idempotency-Key.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import invariants, queries, services
from app.catalog import (
    archive_warehouse,
    create_product,
    create_warehouse,
    default_shop,
    list_products,
    list_warehouses,
    rename_warehouse,
    restore_warehouse,
    warehouse_payload,
    warehouse_stock_detail,
)
from app.db import get_session
from app.documents import transition
from app.models import DocKind, DocStatus
from app.schemas import (
    FboIn,
    FbsIn,
    ProductIn,
    ReceiptIn,
    TransferIn,
    WarehouseIn,
    WarehouseRename,
)
from app.seed import reset_demo_data

router = APIRouter(prefix="/api")

IdempotencyKey = Header(default=None, alias="Idempotency-Key")


def _result(response: Response, data: Any, replayed: bool, created: bool = False) -> Any:
    response.status_code = 200 if replayed or not created else 201
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return data


# ---------------------------------------------------------------- служебное


@router.get("/health", tags=["service"])
def health(session: Session = Depends(get_session)) -> dict[str, Any]:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}


@router.get("/meta", tags=["service"])
def meta(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Всё, что нужно интерфейсу для выпадающих списков."""
    shop = default_shop(session)
    return {
        "shop": {"id": shop.id, "name": shop.name},
        "warehouses": list_warehouses(session),
        "products": list_products(session),
        "doc_kinds": [k.value for k in DocKind],
        "doc_statuses": [s.value for s in DocStatus],
    }


@router.get("/invariants", tags=["service"])
def check_invariants(session: Session = Depends(get_session)) -> dict[str, Any]:
    return invariants.check_all(session)


@router.post("/demo/reset", tags=["service"])
def demo_reset(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Пересоздать тестовые данные из ТЗ (полностью очищает базу)."""
    return reset_demo_data(session)


# ---------------------------------------------------------------- остатки


@router.get("/stock", tags=["stock"])
def stock(
    include_archived: bool = False,
    search: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return queries.stock_matrix(session, include_archived=include_archived, search=search)


@router.get("/stock/{sku}/where", tags=["stock"])
def where(sku: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    return queries.where_is(session, sku)


# ---------------------------------------------------------------- справочники


@router.get("/warehouses", tags=["warehouses"])
def warehouses(
    include_archived: bool = True, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    return list_warehouses(session, include_archived=include_archived)


@router.post("/warehouses", tags=["warehouses"])
def add_warehouse(
    payload: WarehouseIn,
    response: Response,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    wh = create_warehouse(session, payload.name, payload.shop_id)
    response.status_code = 201
    return warehouse_payload(session, wh)


@router.patch("/warehouses/{warehouse_id}", tags=["warehouses"])
def patch_warehouse(
    warehouse_id: int, payload: WarehouseRename, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return warehouse_payload(session, rename_warehouse(session, warehouse_id, payload.name))


@router.delete("/warehouses/{warehouse_id}", tags=["warehouses"])
def delete_warehouse(warehouse_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Удаление = архивирование. Остатки и история сохраняются."""
    return archive_warehouse(session, warehouse_id)


@router.post("/warehouses/{warehouse_id}/restore", tags=["warehouses"])
def undelete_warehouse(
    warehouse_id: int, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return warehouse_payload(session, restore_warehouse(session, warehouse_id))


@router.get("/warehouses/{warehouse_id}/stock", tags=["warehouses"])
def warehouse_stock(
    warehouse_id: int, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    return warehouse_stock_detail(session, warehouse_id)


@router.get("/products", tags=["products"])
def products(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return list_products(session)


@router.post("/products", tags=["products"])
def add_product(
    payload: ProductIn, response: Response, session: Session = Depends(get_session)
) -> dict[str, Any]:
    product = create_product(session, payload.sku, payload.name)
    response.status_code = 201
    return {"id": product.id, "sku": product.sku, "name": product.name}


# ---------------------------------------------------------------- приход


@router.post("/receipts", tags=["receipts"])
def new_receipt(
    payload: ReceiptIn,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    data, replayed = services.create_receipt(session, payload.model_dump(), idempotency_key)
    return _result(response, data, replayed, created=True)


@router.post("/receipts/{document_id}/post", tags=["receipts"])
def post_receipt(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.POSTED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.RECEIPT.value,
    )
    return _result(response, result.payload, result.replayed)


# ---------------------------------------------------------------- перемещения


@router.post("/transfers", tags=["transfers"])
def new_transfer(
    payload: TransferIn,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    data, replayed = services.create_transfer(session, payload.model_dump(), idempotency_key)
    return _result(response, data, replayed, created=True)


@router.post("/transfers/quick", tags=["transfers"])
def new_quick_transfer(
    payload: TransferIn,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    """Создать + отправить + принять одной транзакцией (закрыть нехватку в один клик)."""
    data, replayed = services.quick_transfer(session, payload.model_dump(), idempotency_key)
    return _result(response, data, replayed, created=True)


@router.post("/transfers/{document_id}/ship", tags=["transfers"])
def ship_transfer(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.SHIPPED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.TRANSFER.value,
    )
    return _result(response, result.payload, result.replayed)


@router.post("/transfers/{document_id}/receive", tags=["transfers"])
def receive_transfer(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.RECEIVED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.TRANSFER.value,
    )
    return _result(response, result.payload, result.replayed)


@router.post("/transfers/{document_id}/cancel", tags=["transfers"])
def cancel_transfer(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.CANCELLED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.TRANSFER.value,
    )
    return _result(response, result.payload, result.replayed)


# ---------------------------------------------------------------- FBS


@router.post("/fbs/orders", tags=["fbs"])
def new_fbs_order(
    payload: FbsIn,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    data, replayed = services.create_fbs(session, payload.model_dump(), idempotency_key)
    return _result(response, data, replayed, created=True)


@router.post("/fbs/orders/{document_id}/ship", tags=["fbs"])
def ship_fbs(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.SHIPPED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.FBS.value,
    )
    return _result(response, result.payload, result.replayed)


@router.post("/fbs/orders/{document_id}/deliver", tags=["fbs"])
def deliver_fbs(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.DELIVERED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.FBS.value,
    )
    return _result(response, result.payload, result.replayed)


@router.post("/fbs/orders/{document_id}/cancel", tags=["fbs"])
def cancel_fbs(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.CANCELLED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.FBS.value,
    )
    return _result(response, result.payload, result.replayed)


# ---------------------------------------------------------------- FBO


@router.post("/fbo/shipments", tags=["fbo"])
def new_fbo(
    payload: FboIn,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    data, replayed = services.create_fbo(session, payload.model_dump(), idempotency_key)
    return _result(response, data, replayed, created=True)


@router.post("/fbo/shipments/{document_id}/ship", tags=["fbo"])
def ship_fbo(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.SHIPPED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.FBO.value,
    )
    return _result(response, result.payload, result.replayed)


@router.post("/fbo/shipments/{document_id}/receive", tags=["fbo"])
def receive_fbo(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.RECEIVED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.FBO.value,
    )
    return _result(response, result.payload, result.replayed)


@router.post("/fbo/shipments/{document_id}/cancel", tags=["fbo"])
def cancel_fbo(
    document_id: int,
    response: Response,
    session: Session = Depends(get_session),
    idempotency_key: str | None = IdempotencyKey,
) -> dict[str, Any]:
    result = transition(
        session,
        document_id,
        DocStatus.CANCELLED.value,
        idempotency_key=idempotency_key,
        expected_kind=DocKind.FBO.value,
    )
    return _result(response, result.payload, result.replayed)


# ---------------------------------------------------------------- документы и история


@router.get("/documents", tags=["documents"])
def documents(
    kind: str | None = None,
    status: str | None = None,
    warehouse_id: int | None = None,
    sku: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    return queries.list_documents(session, kind, status, warehouse_id, sku, limit)


@router.get("/history", tags=["documents"])
def history(
    kind: str | None = None,
    warehouse_id: int | None = None,
    sku: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    return queries.history(session, limit, kind, warehouse_id, sku)

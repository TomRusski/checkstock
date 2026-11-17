"""Сериализация документов и человекочитаемые подписи узлов учёта."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    Bucket,
    DocKind,
    Document,
    Location,
    LocationKind,
    Warehouse,
)

STATUS_LABELS = {
    "draft": "Черновик",
    "posted": "Проведён",
    "reserved": "Резерв FBS",
    "shipped": "В пути",
    "received": "Принят",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}

KIND_LABELS = {
    "receipt": "Приход",
    "transfer": "Перемещение между ФФ",
    "fbs": "FBS-заказ",
    "fbo": "FBO-поставка",
}

# Какие кнопки показывать в интерфейсе для каждого статуса.
ACTION_LABELS = {
    ("receipt", "posted"): "Провести приход",
    ("transfer", "shipped"): "Отправить",
    ("transfer", "received"): "Принять",
    ("transfer", "cancelled"): "Отменить",
    ("fbs", "shipped"): "Отправить клиенту",
    ("fbs", "delivered"): "Доставлен",
    ("fbs", "cancelled"): "Отменить заказ",
    ("fbo", "shipped"): "Отправить на МП",
    ("fbo", "received"): "Принято на МП",
    ("fbo", "cancelled"): "Отменить",
}


def warehouse_ref(wh: Warehouse | None) -> dict[str, Any] | None:
    if wh is None:
        return None
    return {"id": wh.id, "name": wh.name, "archived": wh.is_archived}


def document_payload(session: Session, doc: Document) -> dict[str, Any]:
    from app.documents import allowed_transitions  # локальный импорт: избегаем цикла

    session.refresh(doc, ["lines", "events"])
    from_wh = session.get(Warehouse, doc.from_warehouse_id) if doc.from_warehouse_id else None
    to_wh = session.get(Warehouse, doc.to_warehouse_id) if doc.to_warehouse_id else None
    actions = [
        {"to_status": s, "label": ACTION_LABELS.get((doc.kind, s), s)}
        for s in allowed_transitions(doc.kind, doc.status)
    ]
    return {
        "id": doc.id,
        "kind": doc.kind,
        "kind_label": KIND_LABELS.get(doc.kind, doc.kind),
        "status": doc.status,
        "status_label": STATUS_LABELS.get(doc.status, doc.status),
        "reference": doc.reference,
        "note": doc.note,
        "from_warehouse": warehouse_ref(from_wh),
        "to_warehouse": warehouse_ref(to_wh),
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "lines": [
            {
                "product_id": line.product_id,
                "sku": line.product.sku,
                "name": line.product.name,
                "qty": line.qty,
            }
            for line in doc.lines
        ],
        "events": [
            {
                "from_status": e.from_status,
                "to_status": e.to_status,
                "at": e.created_at.isoformat() if e.created_at else None,
                "idempotency_key": e.idempotency_key,
            }
            for e in doc.events
        ],
        "actions": actions,
    }


def location_label(
    location: Location,
    bucket: str,
    warehouse: Warehouse | None = None,
    document: Document | None = None,
    warehouses: dict[int, Warehouse] | None = None,
) -> str:
    """Подпись узла для человека. Строится из текущих данных, поэтому
    переименование ФФ сразу отражается и в истории, и в «где числится»."""
    kind = location.kind
    if kind == LocationKind.FULFILLMENT.value:
        name = warehouse.name if warehouse else location.name
        if warehouse is not None and warehouse.is_archived:
            name = f"{name} (архивный ФФ)"
        suffix = "доступно" if bucket == Bucket.FREE.value else "резерв FBS"
        return f"{name} · {suffix}"
    if kind == LocationKind.TRANSIT.value and document is not None:
        wmap = warehouses or {}
        src = wmap.get(document.from_warehouse_id)
        dst = wmap.get(document.to_warehouse_id)
        src_name = src.name if src else "—"
        if document.kind == DocKind.TRANSFER.value:
            dst_name = dst.name if dst else "—"
            return f"В пути между ФФ: {src_name} → {dst_name} (док. #{document.id})"
        if document.kind == DocKind.FBO.value:
            return f"В пути на склад МП: {src_name} → маркетплейс (док. #{document.id})"
        ref = f" заказ {document.reference}" if document.reference else f" док. #{document.id}"
        return f"В пути к клиенту: {src_name} →{ref}"
    return location.name

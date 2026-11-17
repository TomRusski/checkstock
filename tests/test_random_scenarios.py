"""Случайные складские сценарии: 150 действий подряд, сходимость после каждого.

Это ловит то, что не ловят «счастливые пути»: комбинации резервов, отмен,
перемещений, архивирования складов и повторных подтверждений.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import select

from app.accounting import rebuild_projection
from app.models import StockBalance
from tests.helpers import assert_conserved, assert_invariants

pytestmark = pytest.mark.slow

SKUS = ["SKU-101", "SKU-102", "SKU-103"]


def _projection(session):
    return sorted(
        session.execute(
            select(
                StockBalance.product_id,
                StockBalance.location_id,
                StockBalance.bucket,
                StockBalance.qty,
            )
        ).all()
    )


def test_random_scenario_keeps_books_balanced(client, session, wh):
    rng = random.Random(20261117)
    names = list(wh)
    live_docs: list[tuple[str, int]] = []
    server_errors: list[str] = []

    def check(res, allowed=(200, 201, 409, 422, 404)):
        if res.status_code >= 500:
            server_errors.append(
                f"{res.request.method} {res.request.url.path} -> {res.status_code}"
            )
        assert res.status_code in allowed, (
            f"{res.request.url.path}: {res.status_code} {res.text[:200]}"
        )
        return res

    for step in range(150):
        action = rng.choice(
            ["receipt", "transfer", "fbs", "fbo", "advance", "advance", "rename", "archive"]
        )
        sku = rng.choice(SKUS)
        qty = rng.randint(1, 6)
        src, dst = rng.sample(names, 2)

        if action == "receipt":
            res = check(
                client.post(
                    "/api/receipts",
                    json={"to_warehouse_id": wh[dst], "lines": [{"sku": sku, "qty": qty}]},
                )
            )
            if res.status_code == 201:  # склад может быть в архиве — тогда 409, это ожидаемо
                check(client.post(f"/api/receipts/{res.json()['id']}/post"))

        elif action == "transfer":
            res = check(
                client.post(
                    "/api/transfers",
                    json={
                        "from_warehouse_id": wh[src],
                        "to_warehouse_id": wh[dst],
                        "lines": [{"sku": sku, "qty": qty}],
                    },
                )
            )
            if res.status_code == 201:
                live_docs.append(("transfer", res.json()["id"]))

        elif action == "fbs":
            res = check(
                client.post(
                    "/api/fbs/orders",
                    json={
                        "from_warehouse_id": wh[src],
                        "lines": [{"sku": sku, "qty": qty}],
                        "reference": f"WB-{step}",
                        "allow_split": rng.random() < 0.4,
                    },
                )
            )
            if res.status_code == 201:
                for doc in res.json()["documents"]:
                    live_docs.append(("fbs", doc["id"]))

        elif action == "fbo":
            res = check(
                client.post(
                    "/api/fbo/shipments",
                    json={"from_warehouse_id": wh[src], "lines": [{"sku": sku, "qty": qty}]},
                )
            )
            if res.status_code == 201:
                live_docs.append(("fbo", res.json()["id"]))

        elif action == "advance" and live_docs:
            kind, doc_id = rng.choice(live_docs)
            paths = {
                "transfer": ["ship", "receive", "cancel"],
                "fbs": ["ship", "deliver", "cancel"],
                "fbo": ["ship", "receive", "cancel"],
            }[kind]
            base = {"transfer": "transfers", "fbs": "fbs/orders", "fbo": "fbo/shipments"}[kind]
            verb = rng.choice(paths)
            # Иногда повторяем подтверждение с тем же ключом — не должно задваивать.
            key = f"rnd-{kind}-{doc_id}-{verb}" if rng.random() < 0.5 else None
            headers = {"Idempotency-Key": key} if key else {}
            check(client.post(f"/api/{base}/{doc_id}/{verb}", headers=headers))
            if rng.random() < 0.3:
                check(client.post(f"/api/{base}/{doc_id}/{verb}", headers=headers))

        elif action == "rename":
            new_name = f"{src}-{step}"
            res = check(client.patch(f"/api/warehouses/{wh[src]}", json={"name": new_name}))
            if res.status_code == 200:
                wh[new_name] = wh.pop(src)
                names = [new_name if n == src else n for n in names]

        elif action == "archive":
            check(client.delete(f"/api/warehouses/{wh[src]}"))
            if rng.random() < 0.6:
                check(client.post(f"/api/warehouses/{wh[src]}/restore"))

        assert_invariants(session)
        for check_sku in SKUS:
            assert_conserved(session, check_sku)

    assert not server_errors, f"Сервер падал: {server_errors}"

    # Финальная проверка: проекцию можно пересобрать из журнала без расхождений.
    before = _projection(session)
    rebuild_projection(session)
    assert _projection(session) == before
    session.rollback()

    matrix = client.get("/api/stock", params={"include_archived": True}).json()
    for row in matrix["rows"]:
        assert row["accounted_total"] == row["received_total"]

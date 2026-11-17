"""Конкурентность: параллельные подтверждения не должны задваивать списание
и не должны пробивать остаток в минус. Проверка на реальном PostgreSQL.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.helpers import assert_invariants, available, events_count, in_transit, reserved

pytestmark = pytest.mark.slow


def _parallel(fn, n: int):
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(fn, range(n)))


def test_parallel_ship_deducts_once(client, session, wh):
    doc = client.post(
        "/api/transfers",
        json={
            "from_warehouse_id": wh["Север"],
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-101", "qty": 4}],
        },
    ).json()
    responses = _parallel(
        lambda i: client.post(
            f"/api/transfers/{doc['id']}/ship", headers={"Idempotency-Key": f"parallel-ship-{i}"}
        ),
        5,
    )
    codes = [r.status_code for r in responses]
    assert codes.count(200) == 1, f"ожидали ровно один успех, получили {codes}"
    assert set(codes) <= {200, 409}
    assert available(session, "SKU-101", "Север") == 6
    assert in_transit(session, "SKU-101", "transfer") == 4
    assert events_count(session, doc["id"], "shipped") == 1
    assert_invariants(session)


def test_parallel_reserves_never_go_negative(client, session, wh):
    """На Севере 5 шт SKU-102. Шесть параллельных резервов по 2 шт:
    пройти могут только два, остаток не уходит в минус."""
    responses = _parallel(
        lambda i: client.post(
            "/api/fbs/orders",
            json={
                "from_warehouse_id": wh["Север"],
                "lines": [{"sku": "SKU-102", "qty": 2}],
                "reference": f"WB-race-{i}",
            },
        ),
        6,
    )
    successes = [r for r in responses if r.status_code == 201]
    assert all(r.status_code in (201, 409) for r in responses)
    left = available(session, "SKU-102", "Север")
    assert left >= 0
    assert len(successes) * 2 + left == 5
    assert reserved(session, "SKU-102", "Север") == len(successes) * 2
    assert_invariants(session)


def test_parallel_same_key_single_effect(client, session, wh):
    order = client.post(
        "/api/fbs/orders",
        json={"from_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-101", "qty": 3}]},
    ).json()["documents"][0]
    responses = _parallel(
        lambda _: client.post(
            f"/api/fbs/orders/{order['id']}/ship", headers={"Idempotency-Key": "same-key"}
        ),
        5,
    )
    assert all(r.status_code in (200, 409) for r in responses)
    assert any(r.status_code == 200 for r in responses)
    assert events_count(session, order["id"], "shipped") == 1
    assert in_transit(session, "SKU-101", "fbs") == 3
    assert reserved(session, "SKU-101", "Север") == 0
    assert_invariants(session)


def test_parallel_receipt_post_adds_once(client, session, wh):
    doc = client.post(
        "/api/receipts", json={"to_warehouse_id": wh["Юг"], "lines": [{"sku": "SKU-102", "qty": 7}]}
    ).json()
    responses = _parallel(lambda _: client.post(f"/api/receipts/{doc['id']}/post"), 5)
    assert [r.status_code for r in responses].count(200) == 1
    assert available(session, "SKU-102", "Юг") == 7
    assert_invariants(session)

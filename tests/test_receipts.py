"""Приход: черновик не двигает товар, проведение двигает ровно один раз."""

from __future__ import annotations

from tests.helpers import (
    assert_conserved,
    assert_invariants,
    available,
    events_count,
    on_hand,
    postings_count,
    received,
)


def test_seed_matches_assignment(session):
    """Тестовые данные в точности как в ТЗ."""
    assert (on_hand(session, "SKU-101", "Север"), on_hand(session, "SKU-101", "Юг")) == (10, 6)
    assert (on_hand(session, "SKU-102", "Север"), on_hand(session, "SKU-102", "Юг")) == (5, 0)
    assert (on_hand(session, "SKU-103", "Север"), on_hand(session, "SKU-103", "Юг")) == (0, 4)
    assert received(session, "SKU-101") == 16
    assert_invariants(session)


def test_draft_receipt_does_not_change_stock(client, session, wh):
    before = available(session, "SKU-101", "Север")
    res = client.post(
        "/api/receipts",
        json={"to_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-101", "qty": 7}]},
    )
    assert res.status_code == 201
    assert res.json()["status"] == "draft"
    assert available(session, "SKU-101", "Север") == before
    assert postings_count(session, res.json()["id"]) == 0
    assert_invariants(session)


def test_receipt_post_adds_stock_once(client, session, wh):
    doc = client.post(
        "/api/receipts",
        json={
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-102", "qty": 5}, {"sku": "SKU-103", "qty": 2}],
            "reference": "Накладная 1",
        },
    ).json()
    assert client.post(f"/api/receipts/{doc['id']}/post").status_code == 200
    assert available(session, "SKU-102", "Юг") == 5
    assert available(session, "SKU-103", "Юг") == 6
    assert received(session, "SKU-102") == 10
    assert_conserved(session, "SKU-102", expected_received=10)
    assert_invariants(session)

    again = client.post(f"/api/receipts/{doc['id']}/post")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"
    assert available(session, "SKU-102", "Юг") == 5
    assert events_count(session, doc["id"], "posted") == 1


def test_receipt_replay_with_idempotency_key(client, session, wh):
    doc = client.post(
        "/api/receipts",
        json={"to_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-101", "qty": 4}]},
    ).json()
    key = "receipt-post-once"
    first = client.post(f"/api/receipts/{doc['id']}/post", headers={"Idempotency-Key": key})
    second = client.post(f"/api/receipts/{doc['id']}/post", headers={"Idempotency-Key": key})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert first.json() == second.json()
    assert available(session, "SKU-101", "Север") == 14
    assert postings_count(session, doc["id"]) == 2  # одна строка = две проводки
    assert_invariants(session)


def test_receipt_validation(client, wh):
    assert (
        client.post(
            "/api/receipts",
            json={"to_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-101", "qty": 0}]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/receipts",
            json={"to_warehouse_id": wh["Север"], "lines": [{"sku": "NOPE", "qty": 1}]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/receipts",
            json={
                "to_warehouse_id": wh["Север"],
                "lines": [{"sku": "SKU-101", "qty": 1}, {"sku": "SKU-101", "qty": 2}],
            },
        ).status_code
        == 422
    )


def test_history_shows_source_and_destination(client, session, wh):
    doc = client.post(
        "/api/receipts",
        json={"to_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-103", "qty": 3}]},
    ).json()
    client.post(f"/api/receipts/{doc['id']}/post")
    events = client.get("/api/history", params={"sku": "SKU-103", "limit": 5}).json()
    move = events[0]["moves"][0]
    assert move["qty"] == 3
    assert move["from"] == "Поставщик"
    assert move["to"] == "Север · доступно"
    assert_conserved(session, "SKU-103", expected_received=7)

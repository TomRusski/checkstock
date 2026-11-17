"""Идемпотентность: повторное подтверждение не списывает товар дважды."""

from __future__ import annotations

from tests.helpers import assert_invariants, available, events_count, on_hand, postings_count


def test_same_key_returns_same_payload(client, session, wh):
    doc = client.post(
        "/api/transfers",
        json={
            "from_warehouse_id": wh["Север"],
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-101", "qty": 3}],
        },
    ).json()
    key = "ship-transfer-1"
    first = client.post(f"/api/transfers/{doc['id']}/ship", headers={"Idempotency-Key": key})
    second = client.post(f"/api/transfers/{doc['id']}/ship", headers={"Idempotency-Key": key})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert first.json() == second.json()
    assert available(session, "SKU-101", "Север") == 7
    assert events_count(session, doc["id"], "shipped") == 1
    assert postings_count(session, doc["id"]) == 2
    assert_invariants(session)


def test_repeat_without_key_is_conflict(client, session, wh):
    order = client.post(
        "/api/fbs/orders",
        json={"from_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-101", "qty": 4}]},
    ).json()["documents"][0]
    assert client.post(f"/api/fbs/orders/{order['id']}/ship").status_code == 200
    repeat = client.post(f"/api/fbs/orders/{order['id']}/ship")
    assert repeat.status_code == 409
    assert repeat.json()["error"]["details"]["current_status"] == "shipped"
    assert on_hand(session, "SKU-101", "Север") == 6
    assert_invariants(session)


def test_idempotent_creation_of_receipt(client, session, wh):
    payload = {"to_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-101", "qty": 5}]}
    key = "create-receipt-1"
    first = client.post("/api/receipts", json=payload, headers={"Idempotency-Key": key})
    second = client.post("/api/receipts", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    docs = client.get("/api/documents", params={"kind": "receipt"}).json()
    assert len(docs) == 3  # два тестовых прихода + один новый
    assert_invariants(session)


def test_idempotent_creation_of_fbs_order(client, session, wh):
    payload = {
        "from_warehouse_id": wh["Север"],
        "lines": [{"sku": "SKU-101", "qty": 6}],
        "reference": "WB-777",
    }
    key = "create-order-777"
    first = client.post("/api/fbs/orders", json=payload, headers={"Idempotency-Key": key})
    second = client.post("/api/fbs/orders", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()
    assert available(session, "SKU-101", "Север") == 4  # резерв один, не два
    assert_invariants(session)


def test_idempotent_split_creation(client, session, wh):
    payload = {
        "from_warehouse_id": wh["Север"],
        "lines": [{"sku": "SKU-101", "qty": 12}],
        "reference": "WB-888",
        "allow_split": True,
    }
    key = "create-split-888"
    first = client.post("/api/fbs/orders", json=payload, headers={"Idempotency-Key": key})
    second = client.post("/api/fbs/orders", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()
    docs = client.get("/api/documents", params={"kind": "fbs"}).json()
    assert len(docs) == 2
    assert_invariants(session)


def test_key_cannot_be_reused_for_another_document(client, session, wh):
    """Ключ — это идентификатор операции, а не «повтори что-нибудь»."""
    first = client.post(
        "/api/transfers",
        json={
            "from_warehouse_id": wh["Север"],
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-101", "qty": 1}],
        },
    ).json()
    second = client.post(
        "/api/transfers",
        json={
            "from_warehouse_id": wh["Север"],
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-101", "qty": 2}],
        },
    ).json()
    key = "shared-key"
    assert (
        client.post(
            f"/api/transfers/{first['id']}/ship", headers={"Idempotency-Key": key}
        ).status_code
        == 200
    )
    replay = client.post(f"/api/transfers/{second['id']}/ship", headers={"Idempotency-Key": key})
    # Возвращается снимок ПЕРВОЙ операции: второй документ не отправлен.
    assert replay.status_code == 200
    assert replay.json()["id"] == first["id"]
    assert client.get("/api/documents", params={"kind": "transfer"}).json()[0]["status"] == "draft"
    assert available(session, "SKU-101", "Север") == 9
    assert_invariants(session)


def test_receipt_post_key_is_scoped_to_operation(client, session, wh):
    doc = client.post(
        "/api/receipts",
        json={"to_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-102", "qty": 2}]},
    ).json()
    key = "post-once"
    for _ in range(3):
        res = client.post(f"/api/receipts/{doc['id']}/post", headers={"Idempotency-Key": key})
        assert res.status_code == 200
    assert available(session, "SKU-102", "Север") == 7
    assert events_count(session, doc["id"], "posted") == 1
    assert_invariants(session)

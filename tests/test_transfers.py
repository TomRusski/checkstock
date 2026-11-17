"""Перемещения между ФФ: отправка уводит товар «в пути», приёмка возвращает на склад."""

from __future__ import annotations

from tests.helpers import (
    assert_conserved,
    assert_invariants,
    available,
    events_count,
    in_transit,
    on_hand,
)


def _create(client, wh, qty=4, sku="SKU-101"):
    return client.post(
        "/api/transfers",
        json={
            "from_warehouse_id": wh["Север"],
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": sku, "qty": qty}],
            "reference": "Перемещение 1",
        },
    ).json()


def test_full_transfer_cycle(client, session, wh):
    doc = _create(client, wh, qty=4)
    assert doc["status"] == "draft"
    assert available(session, "SKU-101", "Север") == 10

    shipped = client.post(f"/api/transfers/{doc['id']}/ship")
    assert shipped.status_code == 200
    assert available(session, "SKU-101", "Север") == 6
    assert in_transit(session, "SKU-101", "transfer") == 4
    assert on_hand(session, "SKU-101", "Юг") == 6
    assert_conserved(session, "SKU-101", expected_received=16)
    assert_invariants(session)

    received = client.post(f"/api/transfers/{doc['id']}/receive")
    assert received.status_code == 200
    assert in_transit(session, "SKU-101", "transfer") == 0
    assert on_hand(session, "SKU-101", "Юг") == 10
    assert on_hand(session, "SKU-101", "Север") == 6
    assert_conserved(session, "SKU-101", expected_received=16)
    assert_invariants(session)


def test_cannot_receive_before_shipping(client, session, wh):
    doc = _create(client, wh)
    res = client.post(f"/api/transfers/{doc['id']}/receive")
    assert res.status_code == 409
    assert in_transit(session, "SKU-101") == 0
    assert_invariants(session)


def test_double_receive_does_not_duplicate_stock(client, session, wh):
    doc = _create(client, wh, qty=3)
    client.post(f"/api/transfers/{doc['id']}/ship")
    assert client.post(f"/api/transfers/{doc['id']}/receive").status_code == 200
    assert client.post(f"/api/transfers/{doc['id']}/receive").status_code == 409
    assert on_hand(session, "SKU-101", "Юг") == 9
    assert events_count(session, doc["id"], "received") == 1
    assert_invariants(session)


def test_cancel_draft_keeps_stock(client, session, wh):
    doc = _create(client, wh)
    assert client.post(f"/api/transfers/{doc['id']}/cancel").status_code == 200
    assert available(session, "SKU-101", "Север") == 10
    assert in_transit(session, "SKU-101") == 0
    assert_invariants(session)


def test_cannot_cancel_shipped_transfer(client, session, wh):
    """Осознанное ограничение: товар в пути нельзя «отменить», его надо принять.
    Иначе исчезнет физическая ответственность за груз."""
    doc = _create(client, wh)
    client.post(f"/api/transfers/{doc['id']}/ship")
    res = client.post(f"/api/transfers/{doc['id']}/cancel")
    assert res.status_code == 409
    assert in_transit(session, "SKU-101", "transfer") == 4
    assert_invariants(session)


def test_transfer_shortage_is_explained(client, session, wh):
    doc = _create(client, wh, qty=15)  # на Севере только 10, всего 16
    res = client.post(f"/api/transfers/{doc['id']}/ship")
    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "INSUFFICIENT_STOCK"
    assert error["details"]["available_on_warehouse"] == 10
    assert error["details"]["total_available"] == 16
    assert error["details"]["transfer_plan"][0]["from_name"] == "Юг"
    assert available(session, "SKU-101", "Север") == 10
    assert_invariants(session)


def test_transfer_to_same_warehouse_rejected(client, wh):
    res = client.post(
        "/api/transfers",
        json={
            "from_warehouse_id": wh["Север"],
            "to_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-101", "qty": 1}],
        },
    )
    assert res.status_code == 422


def test_quick_transfer_is_atomic_and_complete(client, session, wh):
    res = client.post(
        "/api/transfers/quick",
        json={
            "from_warehouse_id": wh["Юг"],
            "to_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-103", "qty": 4}],
        },
    )
    assert res.status_code == 201
    assert res.json()["status"] == "received"
    assert on_hand(session, "SKU-103", "Север") == 4
    assert on_hand(session, "SKU-103", "Юг") == 0
    assert events_count(session, res.json()["id"], "shipped") == 1
    assert events_count(session, res.json()["id"], "received") == 1
    assert_conserved(session, "SKU-103", expected_received=4)
    assert_invariants(session)


def test_transit_is_visible_per_document(client, session, wh):
    doc = _create(client, wh, qty=2)
    client.post(f"/api/transfers/{doc['id']}/ship")
    where = client.get("/api/stock/SKU-101/where").json()
    transit = [p for p in where["positions"] if p["location_kind"] == "transit"]
    assert len(transit) == 1
    assert transit[0]["qty"] == 2
    assert transit[0]["document"]["id"] == doc["id"]
    assert "Север → Юг" in transit[0]["label"]

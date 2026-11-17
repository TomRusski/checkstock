"""FBO: товар покидает ФФ и числится в пути до приёмки на складе маркетплейса."""

from __future__ import annotations

from tests.helpers import (
    assert_conserved,
    assert_invariants,
    available,
    events_count,
    in_transit,
    marketplace,
    on_hand,
)


def _shipment(client, wh, qty=4, sku="SKU-102"):
    return client.post(
        "/api/fbo/shipments",
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": sku, "qty": qty}],
            "reference": "FBO-77",
        },
    ).json()


def test_draft_fbo_does_not_move_stock(client, session, wh):
    doc = _shipment(client, wh)
    assert doc["status"] == "draft"
    assert on_hand(session, "SKU-102", "Север") == 5
    assert_invariants(session)


def test_fbo_ship_and_receive(client, session, wh):
    doc = _shipment(client, wh, qty=4)
    assert client.post(f"/api/fbo/shipments/{doc['id']}/ship").status_code == 200
    assert on_hand(session, "SKU-102", "Север") == 1
    assert in_transit(session, "SKU-102", "fbo") == 4
    assert marketplace(session, "SKU-102") == 0
    assert_conserved(session, "SKU-102", expected_received=5)
    assert_invariants(session)

    assert client.post(f"/api/fbo/shipments/{doc['id']}/receive").status_code == 200
    assert in_transit(session, "SKU-102", "fbo") == 0
    assert marketplace(session, "SKU-102") == 4
    assert_conserved(session, "SKU-102", expected_received=5)
    assert_invariants(session)


def test_double_receive_fbo(client, session, wh):
    doc = _shipment(client, wh, qty=2)
    client.post(f"/api/fbo/shipments/{doc['id']}/ship")
    assert client.post(f"/api/fbo/shipments/{doc['id']}/receive").status_code == 200
    assert client.post(f"/api/fbo/shipments/{doc['id']}/receive").status_code == 409
    assert marketplace(session, "SKU-102") == 2
    assert events_count(session, doc["id"], "received") == 1
    assert_invariants(session)


def test_cancel_draft_fbo(client, session, wh):
    doc = _shipment(client, wh)
    assert client.post(f"/api/fbo/shipments/{doc['id']}/cancel").status_code == 200
    assert available(session, "SKU-102", "Север") == 5
    assert_invariants(session)


def test_fbo_shortage_on_ship(client, session, wh):
    doc = _shipment(client, wh, qty=50)
    res = client.post(f"/api/fbo/shipments/{doc['id']}/ship")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert res.json()["error"]["details"]["total_available"] == 5
    assert res.json()["error"]["details"]["enough_in_total"] is False
    assert on_hand(session, "SKU-102", "Север") == 5
    assert_invariants(session)


def test_marketplace_stock_is_shown_separately(client, session, wh):
    doc = _shipment(client, wh, qty=3)
    client.post(f"/api/fbo/shipments/{doc['id']}/ship")
    client.post(f"/api/fbo/shipments/{doc['id']}/receive")
    row = next(r for r in client.get("/api/stock").json()["rows"] if r["sku"] == "SKU-102")
    assert row["marketplace"] == 3
    assert row["totals"]["on_hand"] == 2
    assert row["in_transit"]["to_marketplace"] == 0
    assert row["accounted_total"] == row["received_total"] == 5

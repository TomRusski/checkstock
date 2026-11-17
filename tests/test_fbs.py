"""FBS: резерв блокирует товар на ФФ, отправка уводит к клиенту, отмена возвращает."""

from __future__ import annotations

from tests.helpers import (
    assert_conserved,
    assert_invariants,
    available,
    delivered,
    events_count,
    in_transit,
    on_hand,
    reserved,
)


def _order(client, wh, qty=3, sku="SKU-101", reference="WB-1001", **extra):
    return client.post(
        "/api/fbs/orders",
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": sku, "qty": qty}],
            "reference": reference,
            **extra,
        },
    )


def test_reserve_blocks_but_does_not_remove_stock(client, session, wh):
    res = _order(client, wh, qty=3)
    assert res.status_code == 201
    assert res.json()["documents"][0]["status"] == "reserved"
    assert available(session, "SKU-101", "Север") == 7
    assert reserved(session, "SKU-101", "Север") == 3
    assert on_hand(session, "SKU-101", "Север") == 10  # физически товар ещё на складе
    assert in_transit(session, "SKU-101") == 0
    assert_conserved(session, "SKU-101", expected_received=16)
    assert_invariants(session)


def test_full_fbs_cycle(client, session, wh):
    doc = _order(client, wh, qty=4).json()["documents"][0]
    ship = client.post(f"/api/fbs/orders/{doc['id']}/ship")
    assert ship.status_code == 200
    assert reserved(session, "SKU-101", "Север") == 0
    assert on_hand(session, "SKU-101", "Север") == 6
    assert in_transit(session, "SKU-101", "fbs") == 4
    assert_invariants(session)

    deliver = client.post(f"/api/fbs/orders/{doc['id']}/deliver")
    assert deliver.status_code == 200
    assert in_transit(session, "SKU-101", "fbs") == 0
    assert delivered(session, "SKU-101") == 4
    assert_conserved(session, "SKU-101", expected_received=16)
    assert_invariants(session)


def test_cancel_reserve_returns_stock(client, session, wh):
    doc = _order(client, wh, qty=5).json()["documents"][0]
    assert available(session, "SKU-101", "Север") == 5
    assert client.post(f"/api/fbs/orders/{doc['id']}/cancel").status_code == 200
    assert available(session, "SKU-101", "Север") == 10
    assert reserved(session, "SKU-101", "Север") == 0
    assert_invariants(session)


def test_cannot_cancel_after_shipping(client, session, wh):
    doc = _order(client, wh, qty=2).json()["documents"][0]
    client.post(f"/api/fbs/orders/{doc['id']}/ship")
    assert client.post(f"/api/fbs/orders/{doc['id']}/cancel").status_code == 409
    assert in_transit(session, "SKU-101", "fbs") == 2
    assert_invariants(session)


def test_double_ship_does_not_deduct_twice(client, session, wh):
    doc = _order(client, wh, qty=3).json()["documents"][0]
    assert client.post(f"/api/fbs/orders/{doc['id']}/ship").status_code == 200
    assert client.post(f"/api/fbs/orders/{doc['id']}/ship").status_code == 409
    assert on_hand(session, "SKU-101", "Север") == 7
    assert in_transit(session, "SKU-101", "fbs") == 3
    assert events_count(session, doc["id"], "shipped") == 1
    assert_invariants(session)


def test_cannot_deliver_before_shipping(client, session, wh):
    doc = _order(client, wh, qty=1).json()["documents"][0]
    assert client.post(f"/api/fbs/orders/{doc['id']}/deliver").status_code == 409
    assert reserved(session, "SKU-101", "Север") == 1
    assert_invariants(session)


def test_reserved_stock_is_not_available_for_another_order(client, session, wh):
    _order(client, wh, qty=8, reference="WB-1")
    res = _order(client, wh, qty=5, reference="WB-2")
    assert res.status_code == 409
    assert res.json()["error"]["details"]["available_on_warehouse"] == 2
    assert reserved(session, "SKU-101", "Север") == 8
    assert_invariants(session)


def test_reserved_stock_is_not_available_for_fbo(client, session, wh):
    _order(client, wh, qty=9)
    res = client.post(
        "/api/fbo/shipments",
        json={"from_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-101", "qty": 5}]},
    )
    doc = res.json()
    ship = client.post(f"/api/fbo/shipments/{doc['id']}/ship")
    assert ship.status_code == 409
    assert ship.json()["error"]["details"]["available_on_warehouse"] == 1
    assert_invariants(session)


def test_multi_line_order(client, session, wh):
    res = client.post(
        "/api/fbs/orders",
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-101", "qty": 2}, {"sku": "SKU-102", "qty": 3}],
            "reference": "WB-77",
        },
    )
    assert res.status_code == 201
    assert available(session, "SKU-101", "Север") == 8
    assert available(session, "SKU-102", "Север") == 2
    assert reserved(session, "SKU-102", "Север") == 3
    assert_invariants(session)

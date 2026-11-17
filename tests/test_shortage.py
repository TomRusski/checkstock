"""Нехватка на выбранном ФФ при достаточном общем остатке (кейс из ТЗ).

Проверяем три вещи: понятный отказ с раскладкой, закрытие дефицита перемещением
 и разбивку отгрузки по складам.
"""

from __future__ import annotations

from tests.helpers import assert_invariants, available, on_hand, reserved


def test_shortage_response_contains_full_breakdown(client, session, wh):
    res = client.post(
        "/api/fbs/orders",
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-101", "qty": 12}],
            "reference": "WB-500",
        },
    )
    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "INSUFFICIENT_STOCK"
    details = error["details"]
    assert details["requested"] == 12
    assert details["available_on_warehouse"] == 10
    assert details["shortfall"] == 2
    assert details["total_available"] == 16
    assert details["enough_in_total"] is True
    assert details["other_warehouses"] == [{"warehouse_id": wh["Юг"], "name": "Юг", "available": 6}]
    assert details["transfer_plan"] == [
        {
            "from_warehouse_id": wh["Юг"],
            "from_name": "Юг",
            "to_warehouse_id": wh["Север"],
            "to_name": "Север",
            "qty": 2,
        }
    ]
    assert details["split_plan"] == [
        {"warehouse_id": wh["Север"], "name": "Север", "qty": 10},
        {"warehouse_id": wh["Юг"], "name": "Юг", "qty": 2},
    ]
    # Отказ ничего не сломал:
    assert reserved(session, "SKU-101", "Север") == 0
    assert_invariants(session)


def test_shortage_then_transfer_then_reserve(client, session, wh):
    payload = {
        "from_warehouse_id": wh["Север"],
        "lines": [{"sku": "SKU-101", "qty": 12}],
        "reference": "WB-500",
    }
    assert client.post("/api/fbs/orders", json=payload).status_code == 409
    transfer = client.post(
        "/api/transfers/quick",
        json={
            "from_warehouse_id": wh["Юг"],
            "to_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-101", "qty": 2}],
            "reference": "Подпитка под WB-500",
        },
    )
    assert transfer.status_code == 201
    assert available(session, "SKU-101", "Север") == 12
    res = client.post("/api/fbs/orders", json=payload)
    assert res.status_code == 201
    assert reserved(session, "SKU-101", "Север") == 12
    assert available(session, "SKU-101", "Север") == 0
    assert_invariants(session)


def test_split_order_between_warehouses(client, session, wh):
    res = client.post(
        "/api/fbs/orders",
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-101", "qty": 12}],
            "reference": "WB-500",
            "allow_split": True,
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["split"] is True
    assert len(data["documents"]) == 2
    first, second = data["documents"]
    assert first["from_warehouse"]["name"] == "Север"
    assert first["lines"][0]["qty"] == 10
    assert second["from_warehouse"]["name"] == "Юг"
    assert second["lines"][0]["qty"] == 2
    assert first["reference"] == second["reference"] == "WB-500"
    assert reserved(session, "SKU-101", "Север") == 10
    assert reserved(session, "SKU-101", "Юг") == 2
    assert available(session, "SKU-101", "Север") == 0
    assert available(session, "SKU-101", "Юг") == 4
    assert_invariants(session)

    # Обе части можно отправить и доставить — товар доедет целиком.
    for doc in (first, second):
        assert client.post(f"/api/fbs/orders/{doc['id']}/ship").status_code == 200
        assert client.post(f"/api/fbs/orders/{doc['id']}/deliver").status_code == 200
    row = next(r for r in client.get("/api/stock").json()["rows"] if r["sku"] == "SKU-101")
    assert row["delivered"] == 12
    assert row["accounted_total"] == row["received_total"] == 16
    assert_invariants(session)


def test_split_impossible_when_total_is_short(client, session, wh):
    res = client.post(
        "/api/fbs/orders",
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-101", "qty": 100}],
            "allow_split": True,
        },
    )
    assert res.status_code == 409
    details = res.json()["error"]["details"]
    assert details["enough_in_total"] is False
    assert details["split_plan"] == []
    assert reserved(session, "SKU-101", "Север") == 0
    assert_invariants(session)


def test_split_prefers_chosen_warehouse(client, session, wh):
    """Если товара хватает на выбранном ФФ, разбивка не нужна."""
    res = client.post(
        "/api/fbs/orders",
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-101", "qty": 5}],
            "allow_split": True,
        },
    )
    assert res.status_code == 201
    assert res.json()["split"] is False
    assert len(res.json()["documents"]) == 1
    assert on_hand(session, "SKU-101", "Юг") == 6
    assert_invariants(session)


def test_multi_line_split(client, session, wh):
    res = client.post(
        "/api/fbs/orders",
        json={
            "from_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-101", "qty": 8}, {"sku": "SKU-103", "qty": 4}],
            "reference": "WB-900",
            "allow_split": True,
        },
    )
    assert res.status_code == 201
    docs = res.json()["documents"]
    assert len(docs) == 2
    # SKU-103 целиком с Юга, SKU-101: 6 с Юга + 2 с Севера
    by_wh = {
        d["from_warehouse"]["name"]: {line["sku"]: line["qty"] for line in d["lines"]} for d in docs
    }
    assert by_wh["Юг"] == {"SKU-101": 6, "SKU-103": 4}
    assert by_wh["Север"] == {"SKU-101": 2}
    assert_invariants(session)


def test_fbo_shortage_offers_transfer(client, session, wh):
    """Для FBO дефицит тоже объясняется: 5 на Севере, 0 на Юге — сначала приход."""
    doc = client.post(
        "/api/fbo/shipments",
        json={"from_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-103", "qty": 3}]},
    ).json()
    res = client.post(f"/api/fbo/shipments/{doc['id']}/ship")
    assert res.status_code == 409
    details = res.json()["error"]["details"]
    assert details["available_on_warehouse"] == 0
    assert details["total_available"] == 4
    assert details["transfer_plan"][0]["from_name"] == "Юг"
    assert details["transfer_plan"][0]["qty"] == 3
    assert_invariants(session)

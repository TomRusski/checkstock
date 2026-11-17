"""Удаление ФФ с остатком, переименование, архив и восстановление (кейс из ТЗ)."""

from __future__ import annotations

from tests.helpers import assert_invariants, available, in_transit, on_hand, reserved


def test_archive_warehouse_keeps_stock_and_history(client, session, wh):
    # Сначала создаём историю на складе: резерв и отправленное перемещение.
    order = client.post(
        "/api/fbs/orders",
        json={"from_warehouse_id": wh["Юг"], "lines": [{"sku": "SKU-101", "qty": 2}]},
    ).json()["documents"][0]
    transfer = client.post(
        "/api/transfers",
        json={
            "from_warehouse_id": wh["Юг"],
            "to_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-103", "qty": 1}],
        },
    ).json()
    client.post(f"/api/transfers/{transfer['id']}/ship")

    res = client.delete(f"/api/warehouses/{wh['Юг']}")
    assert res.status_code == 200
    assert res.json()["archived"] is True
    assert res.json()["units_on_hand"] == 9  # SKU-101: 4 своб. + 2 резерв, SKU-103: 3

    # Остатки на месте, включая резерв.
    assert on_hand(session, "SKU-101", "Юг") == 6
    assert reserved(session, "SKU-101", "Юг") == 2
    assert on_hand(session, "SKU-103", "Юг") == 3
    assert in_transit(session, "SKU-103", "transfer") == 1

    # Архивный склад виден в остатках и помечен.
    matrix = client.get("/api/stock").json()
    assert any(w["archived"] for w in matrix["warehouses"])
    row = next(r for r in matrix["rows"] if r["sku"] == "SKU-101")
    cell = next(c for c in row["warehouses"] if c["warehouse_id"] == wh["Юг"])
    assert (cell["on_hand"], cell["reserved"]) == (6, 2)

    # «Где числится товар» показывает архивный ФФ.
    where = client.get("/api/stock/SKU-103/where").json()
    assert any("архивный ФФ" in p["label"] for p in where["positions"])

    # История не потерялась.
    events = client.get("/api/history", params={"warehouse_id": wh["Юг"], "limit": 50}).json()
    assert len(events) >= 3
    assert_invariants(session)

    # Начатые документы можно довести до конца.
    assert client.post(f"/api/fbs/orders/{order['id']}/ship").status_code == 200
    assert client.post(f"/api/transfers/{transfer['id']}/receive").status_code == 200
    assert on_hand(session, "SKU-103", "Север") == 1
    assert_invariants(session)


def test_archived_warehouse_rejects_new_documents(client, session, wh):
    client.delete(f"/api/warehouses/{wh['Юг']}")
    receipt = client.post(
        "/api/receipts", json={"to_warehouse_id": wh["Юг"], "lines": [{"sku": "SKU-101", "qty": 1}]}
    )
    assert receipt.status_code == 409
    order = client.post(
        "/api/fbs/orders",
        json={"from_warehouse_id": wh["Юг"], "lines": [{"sku": "SKU-101", "qty": 1}]},
    )
    assert order.status_code == 409
    transfer = client.post(
        "/api/transfers",
        json={
            "from_warehouse_id": wh["Север"],
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-101", "qty": 1}],
        },
    )
    assert transfer.status_code == 409
    assert_invariants(session)


def test_archive_is_idempotent(client, wh):
    first = client.delete(f"/api/warehouses/{wh['Юг']}")
    second = client.delete(f"/api/warehouses/{wh['Юг']}")
    assert first.status_code == second.status_code == 200
    assert first.json()["archived"] is second.json()["archived"] is True


def test_rename_propagates_everywhere(client, session, wh):
    transfer = client.post(
        "/api/transfers",
        json={
            "from_warehouse_id": wh["Север"],
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-101", "qty": 2}],
        },
    ).json()
    client.post(f"/api/transfers/{transfer['id']}/ship")
    assert (
        client.patch(f"/api/warehouses/{wh['Север']}", json={"name": "Север-2"}).status_code == 200
    )

    matrix = client.get("/api/stock").json()
    assert "Север-2" in [w["name"] for w in matrix["warehouses"]]
    where = client.get("/api/stock/SKU-101/where").json()
    assert any("Север-2 → Юг" in p["label"] for p in where["positions"])
    events = client.get("/api/history", params={"sku": "SKU-101", "limit": 3}).json()
    assert "Север-2" in events[0]["moves"][0]["from"]
    assert available(session, "SKU-101", "Север-2") == 8
    assert_invariants(session)


def test_duplicate_active_name_rejected(client, wh):
    assert client.post("/api/warehouses", json={"name": "Север"}).status_code == 409
    assert client.post("/api/warehouses", json={"name": "север"}).status_code == 409
    assert client.post("/api/warehouses", json={"name": "Запад"}).status_code == 201


def test_archived_name_can_be_reused(client, session, wh):
    client.delete(f"/api/warehouses/{wh['Юг']}")
    res = client.post("/api/warehouses", json={"name": "Юг"})
    assert res.status_code == 201
    assert res.json()["id"] != wh["Юг"]
    # Старые остатки остались на старом (архивном) складе, новый пустой.
    assert on_hand(session, "SKU-101", "Юг") == 6  # сумма по обоим узлам с именем Юг
    assert res.json()["units_on_hand"] == 0
    assert_invariants(session)


def test_restore_warehouse(client, wh):
    client.delete(f"/api/warehouses/{wh['Юг']}")
    res = client.post(f"/api/warehouses/{wh['Юг']}/restore")
    assert res.status_code == 200
    assert res.json()["archived"] is False
    assert (
        client.post(
            "/api/fbs/orders",
            json={"from_warehouse_id": wh["Юг"], "lines": [{"sku": "SKU-103", "qty": 1}]},
        ).status_code
        == 201
    )


def test_restore_conflicts_with_active_name(client, wh):
    client.delete(f"/api/warehouses/{wh['Юг']}")
    client.post("/api/warehouses", json={"name": "Юг"})
    assert client.post(f"/api/warehouses/{wh['Юг']}/restore").status_code == 409


def test_warehouse_crud_and_missing(client, wh):
    created = client.post("/api/warehouses", json={"name": "  Запад  "})
    assert created.status_code == 201
    assert created.json()["name"] == "Запад"
    assert (
        client.patch(f"/api/warehouses/{created.json()['id']}", json={"name": "Восток"}).json()[
            "name"
        ]
        == "Восток"
    )
    assert client.delete("/api/warehouses/999999").status_code == 404
    assert client.post("/api/warehouses", json={"name": "   "}).status_code == 422

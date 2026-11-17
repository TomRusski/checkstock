"""Дымовые проверки HTTP-слоя и интерфейса: страница, статика, фильтры, сброс демо."""

from __future__ import annotations

from tests.helpers import assert_invariants, on_hand


def test_index_page_and_static(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "CheckStock" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_openapi_docs_available(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/api/stock" in schema["paths"]
    assert "/api/fbs/orders" in schema["paths"]


def test_health_and_meta(client):
    assert client.get("/api/health").json() == {"status": "ok", "database": "ok"}
    meta = client.get("/api/meta").json()
    assert meta["shop"]["name"]
    assert {w["name"] for w in meta["warehouses"]} == {"Север", "Юг"}
    assert {p["sku"] for p in meta["products"]} == {"SKU-101", "SKU-102", "SKU-103"}


def test_stock_matrix_shape(client):
    data = client.get("/api/stock").json()
    assert len(data["rows"]) == 3
    row = next(r for r in data["rows"] if r["sku"] == "SKU-101")
    assert row["totals"] == {"available": 16, "reserved": 0, "on_hand": 16}
    assert row["in_transit"] == {
        "between_ff": 0,
        "to_marketplace": 0,
        "to_client": 0,
        "total": 0,
    }
    assert row["received_total"] == row["accounted_total"] == 16
    assert data["grand_totals"]["on_hand"] == 25


def test_stock_search_filter(client):
    data = client.get("/api/stock", params={"search": "контей"}).json()
    assert [r["sku"] for r in data["rows"]] == ["SKU-102"]
    assert (
        client.get("/api/stock", params={"search": "SKU-103"}).json()["rows"][0]["name"]
        == "Корзина"
    )


def test_where_is_unknown_sku(client):
    res = client.get("/api/stock/NOPE/where")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_document_and_history_filters(client, wh):
    client.post(
        "/api/fbs/orders",
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-102", "qty": 1}],
            "reference": "WB-filter",
        },
    )
    docs = client.get("/api/documents", params={"kind": "fbs", "status": "reserved"}).json()
    assert len(docs) == 1
    assert docs[0]["reference"] == "WB-filter"
    assert client.get("/api/documents", params={"sku": "SKU-102"}).json()[0]["kind"] == "fbs"
    assert (
        client.get("/api/documents", params={"warehouse_id": wh["Юг"], "kind": "fbs"}).json() == []
    )
    events = client.get("/api/history", params={"kind": "fbs", "limit": 10}).json()
    assert events and events[0]["moves"][0]["sku"] == "SKU-102"


def test_products_endpoint(client):
    created = client.post("/api/products", json={"sku": "SKU-200", "name": "Полка"})
    assert created.status_code == 201
    assert client.post("/api/products", json={"sku": "SKU-200", "name": "Дубль"}).status_code == 409
    assert any(p["sku"] == "SKU-200" for p in client.get("/api/products").json())


def test_demo_reset_restores_assignment_data(client, session, wh):
    client.post(
        "/api/fbs/orders",
        json={"from_warehouse_id": wh["Север"], "lines": [{"sku": "SKU-101", "qty": 9}]},
    )
    client.delete(f"/api/warehouses/{wh['Юг']}")
    assert on_hand(session, "SKU-101", "Север") == 10

    # Сброс делает TRUNCATE (ACCESS EXCLUSIVE). Наша тестовая сессия держала
    # транзакцию после SELECT — освобождаем её, иначе TRUNCATE будет ждать вечно.
    session.commit()
    res = client.post("/api/demo/reset")
    assert res.status_code == 200
    assert len(res.json()["receipts"]) == 2
    assert on_hand(session, "SKU-101", "Север") == 10
    assert on_hand(session, "SKU-101", "Юг") == 6
    assert client.get("/api/documents", params={"kind": "fbs"}).json() == []
    assert {w["name"] for w in client.get("/api/meta").json()["warehouses"]} == {"Север", "Юг"}
    assert_invariants(session)


def test_error_format_is_consistent(client):
    res = client.post(
        "/api/receipts", json={"to_warehouse_id": 999, "lines": [{"sku": "SKU-101", "qty": 1}]}
    )
    assert res.status_code == 404
    body = res.json()
    assert set(body["error"]) == {"code", "message", "details"}


def test_validation_error_format(client):
    res = client.post("/api/receipts", json={"to_warehouse_id": 1, "lines": []})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_FAILED"

"""Сквозной сценарий из ТЗ против запущенного сервера.

Запуск:  make demo   (внутри контейнера приложение всегда на 8000)
         python scripts/demo_scenario.py --base-url http://localhost:18080   # с хоста

Скрипт сам себя проверяет: после каждого шага сверяет остатки и инварианты,
при расхождении падает с ненулевым кодом. Удобно и для проверки, и для видео.
ВНИМАНИЕ: начинает со сброса демо-данных.
"""

from __future__ import annotations

import argparse
import sys

import httpx

GREEN, RED, RESET, BOLD = "\033[32m", "\033[31m", "\033[0m", "\033[1m"


class Demo:
    def __init__(self, base_url: str) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=30)
        self.step_no = 0

    def step(self, title: str) -> None:
        self.step_no += 1
        print(f"\n{BOLD}── Шаг {self.step_no}. {title}{RESET}")

    def call(self, method: str, path: str, *, expect: int = 200, **kwargs):
        res = self.client.request(method, path, **kwargs)
        if res.status_code != expect:
            print(f"{RED}ПРОВАЛ{RESET} {method} {path}: {res.status_code} (ожидали {expect})")
            print(res.text[:800])
            sys.exit(1)
        return res.json() if res.content else None

    def show(self, sku: str) -> dict:
        rows = self.call("GET", "/api/stock", params={"include_archived": True})["rows"]
        row = next(r for r in rows if r["sku"] == sku)
        cells = " | ".join(
            f"{w['name']}{' (архив)' if w['archived'] else ''}: "
            f"физ {w['on_hand']} / дост {w['available']} / резерв {w['reserved']}"
            for w in row["warehouses"]
        )
        print(f"   {sku}: {cells}")
        print(
            f"        в пути: между ФФ {row['in_transit']['between_ff']}, "
            f"на МП {row['in_transit']['to_marketplace']}, "
            f"к клиенту {row['in_transit']['to_client']}"
            f" | склад МП {row['marketplace']} | у клиента {row['delivered']}"
            f" | приход всего {row['received_total']}"
        )
        assert row["accounted_total"] == row["received_total"], "остатки не сходятся!"
        return row

    def invariants(self, label: str = "проверка учёта") -> None:
        report = self.call("GET", "/api/invariants")
        if report["ok"]:
            print(f"   {GREEN}✓ {label}: все {len(report['checks'])} инвариантов выполнены{RESET}")
        else:
            print(f"{RED}✗ {label}: {report}{RESET}")
            sys.exit(1)

    def warehouses(self) -> dict[str, int]:
        return {w["name"]: w["id"] for w in self.call("GET", "/api/warehouses")}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    d = Demo(args.base_url)

    d.step("Сброс и тестовые данные из ТЗ (один магазин, два ФФ, три артикула)")
    d.call("POST", "/api/demo/reset")
    wh = d.warehouses()
    for sku in ("SKU-101", "SKU-102", "SKU-103"):
        d.show(sku)
    d.invariants("после прихода")

    d.step("Приход: +5 SKU-102 на «Юг» (черновик -> проведение)")
    receipt = d.call(
        "POST",
        "/api/receipts",
        expect=201,
        json={
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-102", "qty": 5}],
            "reference": "Накладная 42",
        },
    )
    print(f"   документ #{receipt['id']} в статусе {receipt['status']} — товар ещё не двигался")
    d.call("POST", f"/api/receipts/{receipt['id']}/post")
    d.show("SKU-102")
    d.invariants("после прихода")

    d.step("FBS: резерв 4 шт SKU-101 на «Севере» — товар заблокирован, но физически на складе")
    order = d.call(
        "POST",
        "/api/fbs/orders",
        expect=201,
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-101", "qty": 4}],
            "reference": "WB-1001",
        },
    )["documents"][0]
    row = d.show("SKU-101")
    north = next(w for w in row["warehouses"] if w["name"] == "Север")
    assert (north["on_hand"], north["available"], north["reserved"]) == (10, 6, 4)
    d.invariants("после резерва")

    d.step("FBS: отправка клиенту и повторное подтверждение (идемпотентность)")
    d.call(
        "POST", f"/api/fbs/orders/{order['id']}/ship", headers={"Idempotency-Key": "demo-ship-1001"}
    )
    d.show("SKU-101")
    repeat = d.client.post(
        f"/api/fbs/orders/{order['id']}/ship", headers={"Idempotency-Key": "demo-ship-1001"}
    )
    print(
        f"   повтор с тем же ключом: HTTP {repeat.status_code}, заголовок "
        f"Idempotency-Replayed={repeat.headers.get('Idempotency-Replayed')}"
    )
    assert repeat.status_code == 200
    no_key = d.client.post(f"/api/fbs/orders/{order['id']}/ship")
    print(f"   повтор без ключа: HTTP {no_key.status_code} ({no_key.json()['error']['code']})")
    assert no_key.status_code == 409
    d.show("SKU-101")
    d.call("POST", f"/api/fbs/orders/{order['id']}/deliver")
    print("   доставлено клиенту:")
    d.show("SKU-101")
    d.invariants("после отправки и доставки")

    d.step("FBO: 3 шт SKU-102 покидают «Север» и числятся в пути до приёмки")
    fbo = d.call(
        "POST",
        "/api/fbo/shipments",
        expect=201,
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-102", "qty": 3}],
            "reference": "FBO-77",
        },
    )
    d.call("POST", f"/api/fbo/shipments/{fbo['id']}/ship")
    d.show("SKU-102")
    d.call("POST", f"/api/fbo/shipments/{fbo['id']}/receive")
    print("   принято на складе маркетплейса:")
    d.show("SKU-102")
    d.invariants("после FBO")

    d.step("Нехватка: заказ 12 шт SKU-101 с «Севера», где доступно только 6")
    payload = {
        "from_warehouse_id": wh["Север"],
        "lines": [{"sku": "SKU-101", "qty": 12}],
        "reference": "WB-2002",
    }
    res = d.client.post("/api/fbs/orders", json=payload)
    assert res.status_code == 409, res.text
    details = res.json()["error"]["details"]
    print(f"   {res.json()['error']['message']}")
    print(f"   план перемещения: {details['transfer_plan']}")
    print(f"   план разбивки:    {details['split_plan']}")

    d.step("Вариант А: закрыть дефицит перемещением и повторить заказ")
    for move in details["transfer_plan"]:
        d.call(
            "POST",
            "/api/transfers/quick",
            expect=201,
            json={
                "from_warehouse_id": move["from_warehouse_id"],
                "to_warehouse_id": move["to_warehouse_id"],
                "lines": [{"sku": details["sku"], "qty": move["qty"]}],
                "reference": "Подпитка под WB-2002",
            },
        )
    d.show("SKU-101")
    big_order = d.call("POST", "/api/fbs/orders", expect=201, json=payload)["documents"][0]
    print(f"   заказ WB-2002 зарезервирован: документ #{big_order['id']}")
    d.show("SKU-101")
    d.call("POST", f"/api/fbs/orders/{big_order['id']}/cancel")
    print("   отменили заказ — резерв вернулся в «доступно»:")
    d.show("SKU-101")
    d.invariants("после нехватки и перемещения")

    d.step("Вариант Б: разбить отгрузку по складам (allow_split)")
    split = d.call(
        "POST",
        "/api/fbs/orders",
        expect=201,
        json={
            "from_warehouse_id": wh["Север"],
            "lines": [{"sku": "SKU-102", "qty": 4}],
            "reference": "WB-3003",
            "allow_split": True,
        },
    )
    assert split["split"] is True, "ожидали разбивку на два отправления"
    for doc in split["documents"]:
        print(
            f"   отправление #{doc['id']} с «{doc['from_warehouse']['name']}»: "
            f"{[(line['sku'], line['qty']) for line in doc['lines']]}"
        )
    d.invariants("после разбивки")

    d.step("Перемещение между ФФ по шагам: черновик -> отправлено -> принято")
    transfer = d.call(
        "POST",
        "/api/transfers",
        expect=201,
        json={
            "from_warehouse_id": wh["Север"],
            "to_warehouse_id": wh["Юг"],
            "lines": [{"sku": "SKU-101", "qty": 2}],
            "reference": "Перемещение 5",
        },
    )
    d.call("POST", f"/api/transfers/{transfer['id']}/ship")
    print("   отправлено (товар в пути между ФФ):")
    d.show("SKU-101")
    d.call("POST", f"/api/transfers/{transfer['id']}/receive")
    print("   принято:")
    d.show("SKU-101")
    d.invariants("после перемещения")

    d.step("Удаление ФФ с остатком: «Юг» уходит в архив, товар и история остаются")
    before = d.call("GET", f"/api/warehouses/{wh['Юг']}/stock")
    print(f"   на «Юге» до удаления: {[(r['sku'], r['bucket'], r['qty']) for r in before]}")
    archived = d.call("DELETE", f"/api/warehouses/{wh['Юг']}")
    print(f"   {archived['message']}")
    for sku in ("SKU-101", "SKU-102", "SKU-103"):
        d.show(sku)
    where = d.call("GET", "/api/stock/SKU-103/where")
    print("   где числится SKU-103:")
    for pos in where["positions"]:
        print(f"      {pos['label']}: {pos['qty']}")
    blocked = d.client.post(
        "/api/receipts", json={"to_warehouse_id": wh["Юг"], "lines": [{"sku": "SKU-101", "qty": 1}]}
    )
    print(f"   новый приход на архивный ФФ: HTTP {blocked.status_code} — запрещён")
    assert blocked.status_code == 409
    history = d.call("GET", "/api/history", params={"warehouse_id": wh["Юг"], "limit": 100})
    print(f"   история по «Югу» сохранена: {len(history)} событий")
    assert len(history) > 0
    d.invariants("после удаления ФФ")

    d.step("Итог: приход = всё, что где-либо числится, по каждому артикулу")
    matrix = d.call("GET", "/api/stock", params={"include_archived": True})
    for row in matrix["rows"]:
        print(
            f"   {row['sku']}: принято {row['received_total']} = числится {row['accounted_total']}"
        )
        assert row["received_total"] == row["accounted_total"]
    d.invariants("финальная проверка")
    print(f"\n{GREEN}{BOLD}Сценарий пройден полностью.{RESET}\n")


if __name__ == "__main__":
    main()

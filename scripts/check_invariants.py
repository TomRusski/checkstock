"""Быстрая проверка сходимости остатков на запущенном сервере.

Запуск: make invariants   (внутри контейнера приложение всегда на 8000)
        python scripts/check_invariants.py --base-url http://localhost:18080   # с хоста
Код возврата 0 — всё сходится, 1 — есть нарушения (удобно для CI/скриптов).
"""

from __future__ import annotations

import argparse
import sys

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    report = httpx.get(f"{args.base_url}/api/invariants", timeout=30).json()
    for check in report["checks"]:
        mark = "✓" if check["ok"] else "✗"
        print(f"{mark} {check['name']}: {check['description']}")
        if not check["ok"]:
            for violation in check["violations"]:
                print(f"    {violation}")
    print("\nИтог:", "остатки сходятся" if report["ok"] else "ЕСТЬ РАСХОЖДЕНИЯ")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()

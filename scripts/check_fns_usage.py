#!/usr/bin/env python3
"""
Проверка: сколько запросов к ФНС ЕГРЮЛ (egrul.itsoft.ru) уже сделано сегодня
по legacy-пайплайну обогащения (если он запускался).
Лимит API: 100 запросов/сутки.
"""
from pathlib import Path
import json
from datetime import date

BASE_DIR = Path(__file__).resolve().parents[1]
FNS_USAGE_FILE = BASE_DIR / "data" / "fns_requests_today.json"
FNS_DAILY_LIMIT = 100


def main() -> None:
    today = date.today().isoformat()
    if not FNS_USAGE_FILE.exists():
        print(f"Сегодня ({today}) запросов к ФНС ЕГРЮЛ по нашим запускам: 0")
        print(f"Лимит API egrul.itsoft.ru: {FNS_DAILY_LIMIT}/сутки")
        return
    try:
        data = json.loads(FNS_USAGE_FILE.read_text(encoding="utf-8"))
        if data.get("date") != today:
            print(f"Сегодня ({today}) запросов к ФНС ЕГРЮЛ по нашим запускам: 0")
            print(f"В файле указана дата {data.get('date', '?')}, счётчик обнуляется по дням.")
        else:
            count = int(data.get("count", 0))
            print(f"Сегодня ({today}) запросов к ФНС ЕГРЮЛ по нашим запускам: {count}")
            print(f"Лимит API egrul.itsoft.ru: {FNS_DAILY_LIMIT}/сутки")
            if count >= FNS_DAILY_LIMIT:
                print("  ⚠ Лимит исчерпан, новые запросы до завтра не рекомендуются.")
            elif count >= 95:
                print(f"  Осталось по нашему лимиту в скрипте: {95 - count} (скрипт останавливает ФНС на 95).")
    except Exception as e:
        print(f"Не удалось прочитать счётчик: {e}")


if __name__ == "__main__":
    main()

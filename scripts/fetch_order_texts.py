#!/usr/bin/env python3
"""
Скачивание текстов приказов из БПА Росрыболовства по списку из ody_orders_2023_2026.json.

Важно: берём ИМЕННО «печатную версию» документа (`?docview&page=1&print=1&nd=...`),
а не `?docbody=&nd=...`, потому что последняя содержит только фреймы без самого текста.

Для каждого документа сохраняется текстовая версия печатной страницы в
каталог `data/order_texts/{nd}.txt` (Windows-1251 -> UTF-8).

Скрипт НЕ парсит таблицы, только складывает «сырой» текст, чтобы затем отдельным
скриптом вытащить строки таблиц.
"""

import json
import os
from pathlib import Path
import time
import urllib.request


BASE_DIR = Path(__file__).resolve().parents[1]
JSON_PATH = BASE_DIR / "ody_orders_2023_2026.json"
OUT_DIR = BASE_DIR / "data" / "order_texts"
BASE = "http://92.50.230.187:8080"


def fetch_text(_url: str, nd: str) -> str:
    """
    Забираем текст документа по ссылке вида ?docbody=&nd=... .
    В БПА текст обычно отдаётся как HTML с <pre> или просто как текст.
    Здесь мы не пытаемся его разметить, только сохраняем «как есть».
    """
    # Используем печатную версию документа, где весь текст (включая таблицы)
    # рендерится в одном HTML.
    print_url = f"{BASE}/?docview&page=1&print=1&nd={nd}&rdk=0&&empire="

    req = urllib.request.Request(
        print_url,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()

    # Пытаемся сначала Windows-1251, если не получилось — пробуем UTF-8
    for enc in ("windows-1251", "cp1251", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # если совсем плохо — декодируем с заменой
    return raw.decode("cp1251", errors="replace")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with JSON_PATH.open("r", encoding="utf-8") as f:
        docs = json.load(f)

    print(f"Всего документов в ody_orders_2023_2026.json: {len(docs)}")

    for i, d in enumerate(docs, start=1):
        nd = d.get("nd")
        doc_url = d.get("doc_url")
        if not nd or not doc_url:
            continue

        out_path = OUT_DIR / f"{nd}.txt"
        print(f"[{i}/{len(docs)}] Скачиваю nd={nd} ...")
        try:
            text = fetch_text(doc_url, nd)
        except Exception as e:  # noqa: BLE001
            print(f"  ! Ошибка при скачивании nd={nd}: {e}")
            continue

        out_path.write_text(text, encoding="utf-8")
        # лёгкая задержка, чтобы не спамить сервер
        time.sleep(0.5)

    print("Готово. Тексты приказов сохранены в data/order_texts/")


if __name__ == "__main__":
    main()


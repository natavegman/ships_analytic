#!/usr/bin/env python3
"""
Шаблон: как вытаскивать события освидетельствований из RMRS по IMO.

Источник:
  https://rs-class.org/c/getves.php?imo=<IMO>

Логика:
1) GET страницы судна по IMO.
2) Если в HTML есть "NOT ACCESS" -> у RMRS нет публичного доступа для этого IMO.
3) Иначе парсим:
   - блок "Data for the vessel" (основные реквизиты и class status)
   - блок "Surveys" (события освидетельствований)
4) Складываем в JSON (и при желании грузим в БД).

Запуск:
  python3 scripts/fetch_rmrs_events_template.py --imo 9157820
  python3 scripts/fetch_rmrs_events_template.py --imo 9157820 --out output/rmrs_events_9157820.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import urllib.parse
from pathlib import Path
from typing import Any

import urllib.error
import urllib.request

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = BASE_DIR / "output"
USER_AGENT = "Mozilla/5.0 (compatible; QuotasAnalytic/1.0; +https://rs-class.org)"
RMRS_URL_TEMPLATE = "https://rs-class.org/c/getves.php?imo={imo}"
RMRS_REGBOOK_SEARCH_URL = "https://lk.rs-class.org/regbook/regbookVessel?ln=ru"
RMRS_REGBOOK_VESSEL_URL_TEMPLATE = "https://lk.rs-class.org/regbook/vessel?fleet_id={fleet_id}&ln=ru"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _fetch_url(url: str, timeout_sec: int = 45, post_data: dict[str, str] | None = None) -> str:
    data = None
    if post_data:
        data = urllib.parse.urlencode(post_data).encode("utf-8")
    headers = {"User-Agent": USER_AGENT}
    cookie = os.getenv("RMRS_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            insecure_ctx = ssl._create_unverified_context()  # noqa: SLF001
            with urllib.request.urlopen(req, timeout=timeout_sec, context=insecure_ctx) as resp:
                return resp.read().decode("utf-8", errors="replace")
        raise


def fetch_rmrs_html(imo: str, timeout_sec: int = 45) -> str:
    return _fetch_url(RMRS_URL_TEMPLATE.format(imo=imo), timeout_sec=timeout_sec)


def _extract_table_after_h3(html: str, h3_text: str) -> str:
    m = re.search(rf"<h3>\s*{re.escape(h3_text)}\s*</h3>(.*?)</table>", html, flags=re.S | re.I)
    if not m:
        return ""
    return m.group(1)


def _strip_tags(fragment: str) -> str:
    return _clean_text(re.sub(r"<[^>]+>", " ", fragment))


def parse_vessel_data(html: str) -> dict[str, str]:
    section = _extract_table_after_h3(html, "Data for the vessel")
    if not section:
        return {}
    data: dict[str, str] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", section, flags=re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)
        if len(cells) < 2:
            continue
        for i in range(0, len(cells) - 1, 2):
            key = _strip_tags(cells[i])
            val = _strip_tags(cells[i + 1])
            if key:
                data[key] = val
    return data


def parse_surveys(html: str) -> list[dict[str, str]]:
    section = _extract_table_after_h3(html, "Surveys")
    if not section:
        return []

    headers = [_strip_tags(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", section, flags=re.S | re.I)]
    if not headers:
        headers = ["Type", "Survey", "Code", "Date of last survey", "Date / time the next survey", "Postponement", "Status"]

    rows: list[dict[str, str]] = []
    for tr_match in re.finditer(r"<tr([^>]*)>(.*?)</tr>", section, flags=re.S | re.I):
        tr_attrs = tr_match.group(1) or ""
        tr_body = tr_match.group(2) or ""
        td_cells = re.findall(r"<td[^>]*>(.*?)</td>", tr_body, flags=re.S | re.I)
        if len(td_cells) < 3:
            continue
        row_data: dict[str, str] = {}
        for i, td in enumerate(td_cells):
            key = headers[i] if i < len(headers) else f"col_{i + 1}"
            row_data[key] = _strip_tags(td)
        class_match = re.search(r'class="([^"]+)"', tr_attrs, flags=re.I)
        row_data["row_css_class"] = _clean_text(class_match.group(1) if class_match else "")
        rows.append(row_data)
    return rows


def _search_regbook_fleet_id(imo: str, timeout_sec: int = 45) -> str:
    html = _fetch_url(RMRS_REGBOOK_SEARCH_URL, timeout_sec=timeout_sec, post_data={"namer": imo})
    m = re.search(r'href="vessel\?fleet_id=(\d+)"', html, flags=re.I)
    return m.group(1) if m else ""


def _extract_div_block_by_id(html: str, block_id: str) -> str:
    m = re.search(rf'<div class="tab-pane[^"]*" id="{re.escape(block_id)}"[^>]*>(.*?)</div>\s*</div>', html, flags=re.S | re.I)
    return m.group(1) if m else ""


def parse_regbook_vessel_data(html: str) -> dict[str, str]:
    # В regbook карточке данные судна обычно в tab t0 как пары td/td.
    section = _extract_div_block_by_id(html, "t0")
    if not section:
        section = html
    pairs = re.findall(
        r'<td[^>]*>\s*([^<][^<]*?)\s*</td>\s*<td[^>]*>\s*(.*?)\s*</td>',
        section,
        flags=re.S | re.I,
    )
    ru_to_en = {
        "Название судна": "Name of vessel",
        "Регистровый номер": "RS Number",
        "Номер ИМО": "IMO",
        "Позывной": "Call sign",
        "Порт приписки": "Port of registry",
        "Флаг": "Flag",
        "Символ класса": "RS Class notation",
        "Состояние класса": "Class status",
    }
    out: dict[str, str] = {}
    for raw_key, raw_val in pairs:
        key_ru = _strip_tags(raw_key)
        val = _strip_tags(raw_val)
        if not key_ru:
            continue
        key_en = ru_to_en.get(key_ru)
        if key_en:
            out[key_en] = val
        out[key_ru] = val
    return out


def _extract_rmrs_via_regbook(imo: str) -> dict[str, Any]:
    fleet_id = _search_regbook_fleet_id(imo)
    if not fleet_id:
        return {}
    html = _fetch_url(RMRS_REGBOOK_VESSEL_URL_TEMPLATE.format(fleet_id=fleet_id))
    vessel_data = parse_regbook_vessel_data(html)
    if not vessel_data:
        return {}
    vessel_data.setdefault("IMO", imo)
    return {
        "imo": imo,
        "status": "ok_via_regbook",
        "message": "",
        "fleet_id": fleet_id,
        "source_path": "regbookVessel+vessel",
        "vessel_data": vessel_data,
        "surveys": [],
        "surveys_count": 0,
    }


def extract_rmrs_payload(imo: str) -> dict[str, Any]:
    html = fetch_rmrs_html(imo)
    if "NOT ACCESS" in html:
        fallback = _extract_rmrs_via_regbook(imo)
        if fallback:
            return fallback
        return {
            "imo": imo,
            "status": "not_access",
            "message": "RMRS returned NOT ACCESS for this IMO (likely not in public RS class register).",
            "vessel_data": {},
            "surveys": [],
        }

    vessel_data = parse_vessel_data(html)
    surveys = parse_surveys(html)
    return {
        "imo": imo,
        "status": "ok",
        "message": "",
        "vessel_data": vessel_data,
        "surveys": surveys,
        "surveys_count": len(surveys),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Шаблон выгрузки RMRS-событий по IMO.")
    parser.add_argument("--imo", required=True, help="IMO номер судна.")
    parser.add_argument("--out", default="", help="Путь к JSON-файлу результата.")
    args = parser.parse_args()

    payload = extract_rmrs_payload(args.imo)
    out_path = Path(args.out) if args.out else DEFAULT_OUT_DIR / f"rmrs_events_{args.imo}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DONE] status={payload['status']} surveys={payload.get('surveys_count', 0)} file={out_path}")
    if payload["status"] == "not_access":
        print("[HINT] Добавьте fallback: AIS-посещения верфей + TrustedDocks shipyard visits (если есть доступ).")


if __name__ == "__main__":
    main()

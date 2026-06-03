#!/usr/bin/env python3
"""
Предзагрузка справочника верфей из TrustedDocks для стран RU/KR/CN.

Что делает:
1) Обходит страницы стран:
   - https://www.trusteddocks.com/shipyards/country/ru
   - https://www.trusteddocks.com/shipyards/country/kr
   - https://www.trusteddocks.com/shipyards/country/cn
2) Собирает ссылки на карточки верфей.
3) По каждой карточке вытаскивает JSON-LD (тип LocalBusiness/Organization).
4) Сохраняет результат в CSV.

Запуск:
  python3 scripts/prefetch_trusteddocks_shipyards.py
  python3 scripts/prefetch_trusteddocks_shipyards.py --countries ru kr cn --out data/reference/trusteddocks_shipyards_ru_kr_cn.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import urllib.error
import urllib.request

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUT = BASE_DIR / "data" / "reference" / "trusteddocks_shipyards_ru_kr_cn.csv"
BASE_URL = "https://www.trusteddocks.com"
COUNTRY_URL_TMPL = BASE_URL + "/shipyards/country/{code}"
USER_AGENT = "Mozilla/5.0 (compatible; QuotasAnalytic/1.0; +https://www.trusteddocks.com)"


@dataclass
class ShipyardRow:
    country_code: str
    shipyard_id: str
    shipyard_url: str
    name: str
    address: str
    website: str
    phone: str
    email: str
    lat: str
    lon: str


def _fetch_html(url: str, timeout_sec: int = 45) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            # Рабочий fallback для сред без системного cert store.
            insecure_ctx = ssl._create_unverified_context()  # noqa: SLF001
            with urllib.request.urlopen(req, timeout=timeout_sec, context=insecure_ctx) as resp:
                return resp.read().decode("utf-8", errors="replace")
        raise


def _extract_shipyard_links(country_html: str) -> list[str]:
    links = set(
        re.findall(
            r"https://www\.trusteddocks\.com/shipyards/\d+[a-z0-9\-]*",
            country_html,
            flags=re.IGNORECASE,
        )
    )
    return sorted(links)


def _extract_shipyard_id(url: str) -> str:
    path = urlparse(url).path
    match = re.search(r"/shipyards/(\d+)", path)
    return match.group(1) if match else ""


def _fallback_name_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    last = path.split("/")[-1] if path else ""
    last = re.sub(r"^\d+-", "", last)
    return _compact_space(last.replace("-", " ").upper())


def _compact_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _extract_json_ld_blocks(html: str) -> list[dict]:
    blocks = re.findall(r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>', html, flags=re.S)
    out: list[dict] = []
    for block in blocks:
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _extract_contact_value(label: str, html: str) -> str:
    match = re.search(
        rf"<td>\s*{re.escape(label)}\s*</td>\s*<td>(.*?)</td>",
        html,
        flags=re.IGNORECASE | re.S,
    )
    if not match:
        return ""
    value = re.sub(r"<[^>]+>", " ", match.group(1))
    return _compact_space(value)


def _extract_website_from_html(html: str) -> str:
    match = re.search(r'Website\s*</td>\s*<td>.*?href="([^"]+)"', html, flags=re.IGNORECASE | re.S)
    return _compact_space(match.group(1)) if match else ""


def _parse_shipyard_page(url: str, country_code: str) -> ShipyardRow:
    html = _fetch_html(url)
    data = {}
    for block in _extract_json_ld_blocks(html):
        if block.get("@type") in {"LocalBusiness", "Organization"}:
            data = block
            break

    address = ""
    address_obj = data.get("address")
    if isinstance(address_obj, dict):
        address = _compact_space(address_obj.get("streetAddress", ""))
        if not address:
            address = _compact_space(" ".join(str(v) for v in address_obj.values()))

    geo_obj = data.get("geo") if isinstance(data.get("geo"), dict) else {}

    name = _compact_space(str(data.get("name", ""))) or _fallback_name_from_url(url)

    return ShipyardRow(
        country_code=country_code.upper(),
        shipyard_id=_extract_shipyard_id(url),
        shipyard_url=data.get("url") or url,
        name=name,
        address=address,
        website=_compact_space(_extract_website_from_html(html)),
        phone=_compact_space(_extract_contact_value("Phone", html) or str(data.get("telephone", ""))),
        email=_compact_space(_extract_contact_value("Email", html)),
        lat=_compact_space(str(geo_obj.get("latitude", ""))),
        lon=_compact_space(str(geo_obj.get("longitude", ""))),
    )


def fetch_shipyards(countries: Iterable[str], sleep_sec: float) -> list[ShipyardRow]:
    rows: list[ShipyardRow] = []
    for code_raw in countries:
        code = code_raw.strip().lower()
        if not code:
            continue
        country_url = COUNTRY_URL_TMPL.format(code=code)
        try:
            country_html = _fetch_html(country_url)
        except urllib.error.URLError as exc:
            print(f"[WARN] Не удалось загрузить {country_url}: {exc}")
            continue

        links = _extract_shipyard_links(country_html)
        print(f"[INFO] {code.upper()}: найдено ссылок на верфи: {len(links)}")

        for idx, link in enumerate(links, start=1):
            try:
                row = _parse_shipyard_page(link, code)
                rows.append(row)
                print(f"  [{code.upper()} {idx}/{len(links)}] {row.shipyard_id} {row.name}")
            except urllib.error.URLError as exc:
                print(f"  [WARN] Пропуск {link}: {exc}")
            time.sleep(sleep_sec)
    return rows


def save_rows(rows: list[ShipyardRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "country_code",
        "shipyard_id",
        "shipyard_url",
        "name",
        "address",
        "website",
        "phone",
        "email",
        "lat",
        "lon",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Предзагрузка справочника верфей TrustedDocks.")
    parser.add_argument(
        "--countries",
        nargs="+",
        default=["ru", "kr", "cn"],
        help="Список кодов стран ISO2 (по умолчанию: ru kr cn).",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Куда сохранить CSV (по умолчанию: {DEFAULT_OUT}).",
    )
    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=0.4,
        help="Пауза между запросами карточек верфей.",
    )
    args = parser.parse_args()

    rows = fetch_shipyards(args.countries, max(0.0, args.sleep_sec))
    rows.sort(key=lambda x: (x.country_code, x.name or "", x.shipyard_id))
    save_rows(rows, Path(args.out))
    print(f"[DONE] Сохранено записей: {len(rows)} -> {args.out}")


if __name__ == "__main__":
    main()

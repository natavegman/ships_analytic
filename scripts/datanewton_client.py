#!/usr/bin/env python3
"""
Клиент API DataNewton (datanewton.ru).

Преимущества над egrul.itsoft.ru / list-org / audit-it:
  - Структурированный JSON (без парсинга HTML)
  - /v1/links — полный граф связей до 2-го уровня
  - /v1/batchCards — пакет до 5000 ИНН/ОГРН за один запрос
  - 200 запросов/мин (vs 100/день у ФНС ЕГРЮЛ)
  - Финансы, риски, скоринг

Использование:
    from datanewton_client import DataNewtonClient
    client = DataNewtonClient(api_key="...")  # или из .env DATANEWTON_API_KEY
    info = client.get_counterparty(inn="5190118381")
    links = client.get_links(ogrn="...")
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests


API_BASE = "https://api.datanewton.ru"
RATE_LIMIT_DELAY = 0.35  # 200 req/min ≈ 0.3s between requests


@dataclass
class CompanyData:
    """Parsed company data from DataNewton."""
    inn: str = ""
    ogrn: str = ""
    full_name: str = ""
    short_name: str = ""
    opf: str = ""
    status: str = ""
    is_active: bool = True
    registration_date: str = ""
    address: str = ""
    region: str = ""
    okved_main: str = ""
    okved_main_desc: str = ""
    director: str = ""
    director_inn_fl: str = ""
    director_position: str = ""
    charter_capital: str = ""
    workers_count: str = ""
    founders: list[dict] = field(default_factory=list)
    predecessors: list[dict] = field(default_factory=list)
    successors: list[dict] = field(default_factory=list)
    contacts: str = ""


@dataclass
class LinkNode:
    ogrn: str = ""
    inn: str = ""
    name: str = ""
    node_type: str = ""  # UL, FL, IP
    status: int = 0  # 0=active, 1=inactive


@dataclass
class LinksData:
    """Parsed links graph from DataNewton."""
    root_ogrn: str = ""
    root_inn: str = ""
    nodes: list[LinkNode] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    nodes_count: int = 0
    edges_count: int = 0


class DataNewtonClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("DATANEWTON_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "DATANEWTON_API_KEY not set. "
                "Get a key at https://datanewton.ru and add to .env"
            )
        self._request_count = 0
        self._last_request_time = 0.0

    def _get(self, path: str, params: dict | None = None, retries: int = 3) -> dict | None:
        """Make a GET request to DataNewton API with rate limiting and retries."""
        if params is None:
            params = {}
        params["key"] = self.api_key

        url = f"{API_BASE}{path}"

        for attempt in range(retries):
            elapsed = time.time() - self._last_request_time
            if elapsed < RATE_LIMIT_DELAY:
                time.sleep(RATE_LIMIT_DELAY - elapsed)

            try:
                resp = requests.get(url, params=params, timeout=20)
                self._last_request_time = time.time()
                self._request_count += 1

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 2))
                    print(f"  Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                elif resp.status_code == 409:
                    error = resp.json() if resp.text else {}
                    code = error.get("code", 0)
                    if code == 11:
                        raise ValueError(f"Invalid API key: {error.get('message', '')}")
                    if code == 51:
                        return None  # missing INN/OGRN
                    print(f"  Validation error: {error.get('message', resp.text[:200])}")
                    return None
                elif resp.status_code >= 500:
                    if attempt < retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return None
                else:
                    return None

            except requests.exceptions.Timeout:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None
            except requests.exceptions.ConnectionError:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt * 2)
                    continue
                return None

        return None

    def _post(self, path: str, payload: dict, retries: int = 3) -> dict | None:
        """Make a POST request to DataNewton API."""
        url = f"{API_BASE}{path}"
        params = {"key": self.api_key}

        for attempt in range(retries):
            elapsed = time.time() - self._last_request_time
            if elapsed < RATE_LIMIT_DELAY:
                time.sleep(RATE_LIMIT_DELAY - elapsed)

            try:
                resp = requests.post(
                    url, params=params, json=payload, timeout=30,
                    headers={"Content-Type": "application/json"},
                )
                self._last_request_time = time.time()
                self._request_count += 1

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 2))
                    time.sleep(wait)
                    continue
                elif resp.status_code >= 500 and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                else:
                    return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < retries - 1:
                    time.sleep(2 ** attempt * 2)
                    continue
                return None

        return None

    # ------------------------------------------------------------------
    # Main endpoints
    # ------------------------------------------------------------------

    def get_counterparty(self, inn: str | None = None, ogrn: str | None = None) -> CompanyData | None:
        """Get company info from EGRUL/EGRIP."""
        params = {}
        if inn:
            params["inn"] = inn
        if ogrn:
            params["ogrn"] = ogrn
        if not params:
            return None

        raw = self._get("/v1/counterparty", params)
        if not raw:
            return None
        return self._parse_counterparty(raw)

    def get_links(self, ogrn: str) -> LinksData | None:
        """Get connections graph (directors, founders, related companies)."""
        raw = self._get("/v1/links", {"ogrn": ogrn})
        if not raw:
            return None
        return self._parse_links(raw)

    def get_finance(self, inn: str) -> dict | None:
        """Get financial reports."""
        return self._get("/v1/finance", {"inn": inn})

    def get_batch_cards(self, ogrns: list[str]) -> list[dict]:
        """Get info for multiple companies (up to 5000)."""
        results = []
        for i in range(0, len(ogrns), 5000):
            chunk = ogrns[i:i + 5000]
            raw = self._post("/v1/batchCards", {"ogrns": chunk})
            if raw and isinstance(raw, list):
                results.extend(raw)
            elif raw and "cards" in raw:
                results.extend(raw["cards"])
        return results

    def test_connection(self) -> tuple[bool, str]:
        """Test API connection. Returns (ok, message)."""
        try:
            raw = self._get("/v1/counterparty", {"inn": "7707083893"})
            if raw and raw.get("inn"):
                available = raw.get("available_count", "?")
                return True, f"OK (available: {available})"
            return False, "Unexpected response"
        except ValueError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Error: {e}"

    @property
    def request_count(self) -> int:
        return self._request_count

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_counterparty(raw: dict) -> CompanyData:
        company = raw.get("company", {})
        names = company.get("company_names", {})
        status_block = company.get("status", {})
        addr_block = company.get("address", {})

        # Parse address
        addr_parts = []
        if isinstance(addr_block, dict):
            for key in ("region", "city", "settlement", "street", "house"):
                val = addr_block.get(key, "")
                if val and isinstance(val, str):
                    addr_parts.append(val)
                elif isinstance(val, dict):
                    addr_parts.append(val.get("name", val.get("value", "")))
        address = ", ".join(p for p in addr_parts if p)
        region = ""
        if isinstance(addr_block, dict):
            r = addr_block.get("region", "")
            region = r if isinstance(r, str) else (r.get("name", "") if isinstance(r, dict) else "")

        # Parse managers (directors)
        managers = company.get("managers", []) or []
        director = ""
        director_inn_fl = ""
        director_position = ""
        if managers and isinstance(managers, list):
            mgr = managers[0]
            if isinstance(mgr, dict):
                director = mgr.get("name", mgr.get("fio", ""))
                director_inn_fl = mgr.get("inn", mgr.get("innfl", ""))
                director_position = mgr.get("position", "")

        # Parse owners (founders)
        founders = []
        owners = company.get("owners", {})
        if isinstance(owners, dict):
            for owner_type in ("owners_fl", "owners_ul", "owners_foreign"):
                owner_list = owners.get(owner_type, []) or []
                if isinstance(owner_list, list):
                    for o in owner_list:
                        if isinstance(o, dict):
                            founders.append({
                                "name": o.get("name", o.get("fio", "")),
                                "inn": o.get("inn", o.get("innfl", "")),
                                "share": o.get("share", o.get("nominal_value", "")),
                            })

        # Parse predecessors/successors
        predecessors = []
        for p in (company.get("predecessors") or []):
            if isinstance(p, dict):
                predecessors.append({"inn": p.get("inn", ""), "name": p.get("name", ""), "ogrn": p.get("ogrn", "")})
        successors = []
        for s in (company.get("successors") or []):
            if isinstance(s, dict):
                successors.append({"inn": s.get("inn", ""), "name": s.get("name", ""), "ogrn": s.get("ogrn", "")})

        # Parse OKVED
        okveds = company.get("okveds", []) or []
        okved_main = ""
        okved_main_desc = ""
        if okveds and isinstance(okveds, list):
            for ov in okveds:
                if isinstance(ov, dict) and ov.get("main"):
                    okved_main = ov.get("code", "")
                    okved_main_desc = ov.get("name", "")
                    break

        # Parse contacts
        contacts_block = company.get("contacts", {}) or {}
        contacts_parts = []
        if isinstance(contacts_block, dict):
            for phone in (contacts_block.get("phones") or []):
                if phone:
                    contacts_parts.append(f"Тел: {phone}")
            for email in (contacts_block.get("emails") or []):
                if email:
                    contacts_parts.append(f"Email: {email}")
            for site in (contacts_block.get("sites") or []):
                if site:
                    contacts_parts.append(f"Сайт: {site}")

        # Workers count
        workers = company.get("workers_count")
        if isinstance(workers, dict):
            workers_count = str(workers.get("count", ""))
        elif workers:
            workers_count = str(workers)
        else:
            workers_count = ""

        return CompanyData(
            inn=raw.get("inn", ""),
            ogrn=raw.get("ogrn", ""),
            full_name=names.get("full_name", ""),
            short_name=names.get("short_name", ""),
            opf=company.get("opf", ""),
            status=status_block.get("status_rus_short", ""),
            is_active=status_block.get("active_status", True),
            registration_date=company.get("registration_date", ""),
            address=address,
            region=region,
            okved_main=okved_main,
            okved_main_desc=okved_main_desc,
            director=director,
            director_inn_fl=director_inn_fl,
            director_position=director_position,
            charter_capital=company.get("charter_capital", ""),
            workers_count=workers_count,
            founders=founders,
            predecessors=predecessors,
            successors=successors,
            contacts="; ".join(contacts_parts) if contacts_parts else "",
        )

    @staticmethod
    def _parse_links(raw: dict) -> LinksData:
        nodes = []
        for n in (raw.get("nodes") or []):
            nodes.append(LinkNode(
                ogrn=n.get("ogrn", ""),
                inn=n.get("inn", ""),
                name=n.get("name", ""),
                node_type=n.get("type", ""),
                status=n.get("status", 0),
            ))

        return LinksData(
            root_ogrn=raw.get("ogrn_root", ""),
            root_inn=raw.get("inn_root", ""),
            nodes=nodes,
            edges=raw.get("edges", []),
            nodes_count=raw.get("nodes_count", 0),
            edges_count=raw.get("edges_count", 0),
        )

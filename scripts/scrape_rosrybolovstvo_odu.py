#!/usr/bin/env python3
"""
Scrape ODY (общие допустимые уловы) orders from Rosrybolovstvo NPA search database.
Extracts orders for 2023-2026.
"""
import urllib.request
import re
import json
from html.parser import HTMLParser
from html import unescape

BASE = "http://92.50.230.187:8080"
ENCODED_QUERY = "%EE%E1%F9%E8%F5+%E4%EE%EF%F3%F1%F2%E8%EC%FB%F5+%F3%EB%EE%E2%EE%E2+%F1+2023+%EF%EE+2026+%E3%E3"


def fetch_page(start: int) -> str:
    """Fetch a page of search results."""
    if start == 0:
        url = f"{BASE}/?list_itself=&bpas=v9101&intelsearch={ENCODED_QUERY}&sort=7&page=first"
    else:
        url = f"{BASE}/?list_itself=&bpas=v9101&intelsearch={ENCODED_QUERY}&sort=7&start={start}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("windows-1251")


def parse_document_blocks(html_content: str) -> list[dict]:
    """Parse document blocks from HTML."""
    docs = []
    # Split by list_elem tables
    blocks = re.split(r'<table class="list_elem', html_content)
    
    for block in blocks[1:]:  # skip first empty
        # Extract nd from href in this block
        nd_m = re.search(r'nd=(\d+)', block)
        nd = nd_m.group(1) if nd_m else ""
        
        # Title: Приказ ... от DD.MM.YYYY № N
        title_m = re.search(r'<a id="link_\d+" href="[^"]*"[^>]*>\s*Приказ\s+([^<]+)</a>', block)
        if not title_m:
            continue
        order_part = title_m.group(1)
        date_match = re.search(r'от\s+(\d{2}\.\d{2}\.\d{4})\s+№\s+(\d+)', order_part)
        if not date_match:
            continue
        date_str, number = date_match.groups()
        
        # Description
        desc_m = re.search(r'<span class="bold">([^<]+)</span>', block)
        description = unescape(desc_m.group(1)).strip() if desc_m else ""
        
        # Build URLs - use relative, user will need base
        card_url = f"{BASE}/?docbody=&vkart=card&nd={nd}&bpa=v9101&bpas=v9101"
        doc_url = f"{BASE}/?docbody=&nd={nd}&bpa=v9101&bpas=v9101"
        
        # ODY year from description
        years_in_desc = re.findall(r'на\s+20(2[3-6])\s+год', description)
        years_in_desc += re.findall(r'20(2[3-6])\s+г[\.о]', description)
        years_in_desc += re.findall(r'\(20(2[3-6])\)', description)
        
        organ = "Росрыболовство"  # default for this DB
        if "Минсельхоз" in order_part or "Министерств" in order_part:
            organ = "Минсельхоз"
            
        docs.append({
            "number": number,
            "date": date_str,
            "organ": organ,
            "title_short": f"Приказ от {date_str} № {number}",
            "description": description,
            "ody_years": list(dict.fromkeys(int("20" + y) for y in years_in_desc)),
            "nd": nd,
            "card_url": card_url,
            "doc_url": doc_url,
        })
    
    return docs


def main():
    all_docs = []
    total = 561
    page_size = 20
    
    for start in range(0, total, page_size):
        print(f"Fetching start={start}...", flush=True)
        html_content = fetch_page(start)
        docs = parse_document_blocks(html_content)
        all_docs.extend(docs)
        
    # Filter: only docs that mention 2023, 2024, 2025, or 2026 in context of ODY
    target_years = {2023, 2024, 2025, 2026}
    filtered = []
    for d in all_docs:
        if d["ody_years"]:
            d["ody_years"] = [y for y in d["ody_years"] if y in target_years]
        if d["ody_years"] or any(str(y) in d["description"] for y in target_years):
            # Include if description has "общего допустимого улова" or "общих допустимых уловов"
            if "допустимого улова" in d["description"] or "допустимых уловов" in d["description"]:
                filtered.append(d)
    
    # Sort by date desc
    filtered.sort(key=lambda x: x["date"], reverse=True)
    
    # Save to JSON
    with open("ody_orders_2023_2026.json", "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)
    
    print(f"\nTotal fetched: {len(all_docs)}, filtered for 2023-2026 ODY: {len(filtered)}")
    
    # Fetch a sample card to see attachments structure
    if filtered:
        sample_nd = filtered[0]["nd"]
        card_url = f"{BASE}/?docbody=&vkart=card&nd={sample_nd}&bpa=v9101&bpas=v9101"
        req = urllib.request.Request(card_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            card_html = resp.read().decode("windows-1251")
        # Look for PDF/Excel links
        attach_links = re.findall(r'href="([^"]*\.(?:pdf|xls|xlsx)[^"]*)"', card_html, re.I)
        attach_links += re.findall(r'href="(\?[^"]*attach[^"]*)"', card_html, re.I)
        print(f"Sample card attachments: {attach_links[:5]}")
        with open("sample_card.html", "w", encoding="utf-8") as f:
            f.write(card_html)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build Markdown table from ODY orders JSON."""
import json

with open("ody_orders_2023_2026.json", encoding="utf-8") as f:
    docs = json.load(f)

# Group by year - each doc can appear in multiple years
by_year = {2023: [], 2024: [], 2025: [], 2026: []}
for d in docs:
    years = d.get("ody_years", [])
    if not years:
        # extract year from description
        import re
        m = re.search(r'20(2[3-6])', d["description"])
        if m:
            years = [int("20" + m.group(1))]
    for y in years:
        if y in by_year:
            by_year[y].append(d)

# Sort each year by date desc (newest first)
for year in by_year:
    by_year[year].sort(key=lambda x: (x["date"], x["number"]), reverse=True)

def esc(s):
    return s.replace("|", "\\|").replace("\n", " ")[:120] + ("..." if len(s) > 120 else "")

md = []
md.append("# Приказы об общих допустимых уловах (ОДУ) 2023–2026")
md.append("")
md.append("**Источник:** БПА Росрыболовства, поиск «общих допустимых уловов с 2023 по 2026 гг»")
md.append("**URL поиска:** http://92.50.230.187:8080/?searchres=&bpas=v9101&intelsearch=%EE%E1%F9%E8%F5+%E4%EE%EF%F3%F1%F2%E8%EC%FB%F5+%F3%EB%EE%E2%EE%E2+%F1+2023+%EF%EE+2026+%E3%E3&sort=7")
md.append("")
md.append("**Примечание:** Ссылки на приложения (PDF/Excel) в интерфейсе БПА напрямую не отображаются. Файлы приложений доступны в карточке документа (после открытия карточки — в тексте/приложениях).")
md.append("")
md.append("---")
md.append("")

for year in [2023, 2024, 2025, 2026]:
    md.append(f"## {year} год")
    md.append("")
    md.append("| № приказа | Дата | Орган | Краткое наименование | Год ОДУ | Карточка | Текст |")
    md.append("|-----------|------|-------|----------------------|---------|----------|-------|")
    
    for d in by_year[year]:
        short = d["description"][:80] + "..." if len(d["description"]) > 80 else d["description"]
        short = short.replace("|", "\\|")
        years_str = ",".join(str(y) for y in d["ody_years"]) if d["ody_years"] else "-"
        md.append(f"| {d['number']} | {d['date']} | {d['organ']} | {short} | {years_str} | [Карточка]({d['card_url']}) | [Текст]({d['doc_url']}) |")
    
    md.append("")
    md.append("")

# Add key "Об утверждении" orders section
main_orders = [d for d in docs if d["description"].strip().startswith("Об утверждении")]
main_orders.sort(key=lambda x: (x["ody_years"][0] if x["ody_years"] else 0, x["date"]))

md.append("---")
md.append("")
md.append("## Ключевые приказы об утверждении ОДУ (Минсельхоз / Росрыболовство)")
md.append("")
md.append("| № | Дата | Орган | Наименование | Год | Карточка |")
md.append("|---|------|-------|--------------|-----|----------|")
for d in main_orders:
    years_str = ",".join(str(y) for y in d["ody_years"]) if d["ody_years"] else "-"
    short = (d["description"][:100] + "...") if len(d["description"]) > 100 else d["description"]
    short = short.replace("|", "\\|")
    md.append(f"| {d['number']} | {d['date']} | {d['organ']} | {short} | {years_str} | [Карточка]({d['card_url']}) |")
md.append("")

with open("ODU_orders_2023_2026.md", "w", encoding="utf-8") as f:
    f.write("\n".join(md))

print("Written ODU_orders_2023_2026.md")
print(f"2023: {len(by_year[2023])} docs, 2024: {len(by_year[2024])}, 2025: {len(by_year[2025])}, 2026: {len(by_year[2026])}")
print(f"Main ODY orders: {len(main_orders)}")

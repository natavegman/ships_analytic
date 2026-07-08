#!/usr/bin/env python3
"""
Сверка списка компаний PCA (Pollock Catchers Association, MSC-F-31513, PDF от 25.03.2026)
с базой company_groups_enriched.csv: какие компании уже есть в базе, каких нет.

Транслитерирует Юр_Лицо (кириллица) в латиницу, убирает организационно-правовую форму
с обеих сторон и сравнивает по схожести строк (difflib).

Запуск: python3 scripts/match_pca_companies.py
Результат: output/pca_company_match.csv
"""

from __future__ import annotations

import csv
import re
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
for _k, _v in list(TRANSLIT.items()):
    TRANSLIT[_k.upper()] = _v.upper() if len(_v) == 1 else _v.capitalize()


def to_latin(s: str) -> str:
    return "".join(TRANSLIT.get(c, c) for c in s)


RU_LEGAL_FORMS = re.compile(
    r'\b(ООО|АО|ОАО|ЗАО|ПАО|НАО|ИП|ГУП|МУП|КФХ|СПК|СХПК|ПК|РК|АРТЕЛЬ)\b'
    r'|Общество\s+с\s+ограниченной\s+ответственностью'
    r'|Акционерное\s+общество'
    r'|Публичное\s+акционерное\s+общество'
    r'|Открытое\s+акционерное\s+общество'
    r'|Закрытое\s+акционерное\s+общество'
    r'|Рыболовецкий\s+колхоз',
    re.IGNORECASE,
)
EN_LEGAL_FORMS = re.compile(
    r'\b(JSC|PJSC|LLC|Co\.?|Ltd\.?|LTD|OJSC|CJSC)\b', re.IGNORECASE
)


def normalize(name: str, legal_re: re.Pattern) -> str:
    s = legal_re.sub(" ", name)
    s = re.sub(r'["\'«»,.]', " ", s)
    s = re.sub(r'\s+', " ", s).strip().upper()
    return s


# 34 компании из PDF PCA (в порядке из документа)
PCA_COMPANIES = [
    "AKROS, JSC",
    "AKROS 3, JSC",
    "Collective Farm Fishery by V.I. Lenin",
    "Dalryba, JSC",
    "DMP-RM, JSC",
    "Gavan, LLC",
    "Intraros, JSC",
    "Kamchattralflot, LLC",
    "Kolkhoz im Bekereva, JSC",
    "Kurilskiy Rybak, JSC",
    "MAGADANTRALFLOT, LLC.",
    "Fishing company Malkinskoe, JSC",
    "Mintay DV CO., LTD.",
    "Nakhodka Active Marine Fishery Base (NBAMR), PJSC",
    "Fishing Collective Farm \"Novyi Mir\", JSC",
    "Okeanrybflot, JSC",
    "Ostrov Sakhalin, JSC",
    "Ozernovsky FCP # 55, JSC",
    "Pilenga, JSC",
    "Poseydon, Co., Ltd.",
    "Poronay, LLC",
    "Preobrazhenskaya Basa of Trawling Fleet (PBTF), PJSC",
    "Proliv Co. Ltd",
    "ROLIZ, LLC",
    "Rybak Co. Ltd",
    "PA Sakhalinrybaksoyuz, LLC",
    "Sakhalin Leasing Flot, JSC",
    "Sofco Co., Ltd",
    "Tikhrybcom Co., Ltd.",
    "Tralflot, JSC",
    "TURNIF, JSC",
    "RMD UVA 1, LLC",
    "Fishing company UTRF-Kamchatka, JSC",
    "Vostokrybprom, Co., Ltd.",
]


def word_set(s: str) -> set:
    return {w for w in s.split() if len(w) > 1}


def combined_score(a: str, b: str) -> float:
    wa, wb = word_set(a), word_set(b)
    if not wa or not wb:
        return SequenceMatcher(None, a, b).ratio()
    jaccard = len(wa & wb) / len(wa | wb)
    containment = len(wa & wb) / min(len(wa), len(wb))
    seq = SequenceMatcher(None, a, b).ratio()
    return max(jaccard, 0.6 * containment + 0.4 * seq, seq)


def main():
    enriched_path = ROOT / "data" / "company_groups_enriched.csv"
    with open(enriched_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # предвычислить транслит+нормализацию для базы (основная строка + все упоминания в связях)
    base = []
    for row in rows:
        ru_name = row["Юр_Лицо"]
        norm_ru = normalize(ru_name, RU_LEGAL_FORMS)
        latin = to_latin(norm_ru)
        base.append({"row": row, "latin_norm": latin})

    # компании, упомянутые только внутри графа связей (Связанные_Компании_JSON), но не как отдельная строка
    known_primary_latin = {b["latin_norm"] for b in base}
    related_mentions: dict[str, set] = {}
    rel_name_re = re.compile(r'"name":\s*"((?:[^"\\]|\\.)*)"')
    for row in rows:
        blob = row.get("Связанные_Компании_JSON") or ""
        for m in rel_name_re.finditer(blob):
            name = m.group(1).replace('\\"', '"').strip()
            if not name:
                continue
            norm = normalize(name, RU_LEGAL_FORMS)
            latin = to_latin(norm)
            if latin and latin not in known_primary_latin:
                related_mentions.setdefault(latin, set()).add(row["Группа_Компаний"])

    results = []
    for pca_name in PCA_COMPANIES:
        norm_en = normalize(pca_name, EN_LEGAL_FORMS)
        best = None
        best_score = 0.0
        for b in base:
            score = combined_score(norm_en, b["latin_norm"])
            if score > best_score:
                best_score = score
                best = b

        status = "НЕ НАЙДЕНО" if best_score < 0.45 else ("проверить" if best_score < 0.75 else "совпадает")
        note = ""

        if status == "НЕ НАЙДЕНО":
            # поискать среди упоминаний в связях (графе), но не как отдельная строка
            best_rel = None
            best_rel_score = 0.0
            for latin, groups in related_mentions.items():
                score = combined_score(norm_en, latin)
                if score > best_rel_score:
                    best_rel_score = score
                    best_rel = (latin, groups)
            if best_rel and best_rel_score >= 0.5:
                status = "есть в связях, нет отдельной строки"
                note = f"упомянута в графе связей компаний группы: {', '.join(sorted(best_rel[1]))}"
                best_score = round(best_rel_score, 2)

        results.append({
            "PCA_компания": pca_name,
            "Лучшее_совпадение_в_базе": best["row"]["Юр_Лицо"] if best else "",
            "ИНН": best["row"]["ИНН"] if best else "",
            "Группа_Компаний": best["row"]["Группа_Компаний"] if best else "",
            "Схожесть": round(best_score, 2) if best else 0,
            "Статус": status,
            "Комментарий": note,
        })

    out_path = ROOT / "output" / "pca_company_match.csv"
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    print(f"Записано {len(results)} строк в {out_path}")
    for r in results:
        print(f"[{r['Статус']:10}] {r['Схожесть']:.2f}  {r['PCA_компания']:55} -> {r['Лучшее_совпадение_в_базе']}")


if __name__ == "__main__":
    main()

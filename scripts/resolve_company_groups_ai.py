#!/usr/bin/env python3
"""
ИИ-разбор групп компаний: разрешение конфликтов по директору и назначение
групп там, где их не хватает.

Встраивается в существующий пайплайн между enrich_via_datanewton.py (правило-based
propagation: по директору/учредителям/графу связей) и
export_company_groups_for_manual_edit.py (финальная ручная проверка).
propagate_by_director() честно останавливается там, где один директор
формально относится к нескольким известным группам — это то, что не может
решить правило, но может решить рассуждение с учётом адреса, учредителей и
связанных компаний. Здесь это делает LLM, строго на входных данных (без
домыслов), а неоднозначные случаи явно помечает на ручную проверку — вместо
того чтобы гадать.

Источник данных:
  data/company_groups_enriched.csv — если есть (полные поля от DataNewton).
  data/company_groups.csv          — иначе (только уже сгруппированные строки;
                                      можно только разобрать конфликты в
                                      существующих группах, не назначить новые).

Использование:
    python3 scripts/resolve_company_groups_ai.py --dry-run   # без вызовов API
    python3 scripts/resolve_company_groups_ai.py              # полный прогон
    python3 scripts/resolve_company_groups_ai.py --limit 20

Требует OPENAI_API_KEY в .env (тот же ключ, что и для AI Quota Competitor Monitor).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parents[1]
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
GROUPS_CSV = BASE_DIR / "data" / "company_groups.csv"

ENRICHED_FIELDS = [
    "Группа_Компаний", "Юр_Лицо", "ИНН", "ОГРН", "Комментарий", "Исключить",
    "Контакты_ListOrg", "Директор_ListOrg", "Директор_ИНН_ФЛ",
    "Учредители_JSON", "Связанные_Компании_JSON", "ListOrg_URL",
    "Финансовые_Данные_JSON",
]
GROUPS_FIELDS = [
    "Группа_Компаний", "Юр_Лицо", "ИНН", "Комментарий",
    "Контакты_ListOrg", "Директор_ListOrg", "ListOrg_URL",
]

# Захламлённый формат старого скрипта, повторяющийся десятки раз в одной ячейке:
# "Конфликт групп по директору (ИМЯ): Группа1, Группа2; Конфликт групп по ...; ..."
CONFLICT_RE = re.compile(r"Конфликт групп по директору \(([^)]+)\):\s*([^;]+)")

# Наши собственные пометки — вырезаются перед повторной обработкой строки,
# чтобы повторные запуски перезаписывали, а не копили пометку раз за разом.
AI_NOTE_RE = re.compile(r"\s*(?:Требует проверки \(ИИ\)|\[ИИ,?[^\]]*\]):.*$", re.DOTALL)

SYSTEM_PROMPT = (
    "Ты эксперт по корпоративным структурам рыбопромыслового флота РФ. "
    "Тебе нужно определить, к какой группе компаний (холдингу) относится каждое "
    "юрлицо из списка.\n\n"
    "ЗАПРЕЩЕНО придумывать факты, названия групп или связи, которых нет во входе. "
    "Используй только: директора, ИНН директора, адрес, учредителей, связанные "
    "компании (реорганизации), кандидатные группы конфликта и список известных "
    "групп с примерами их компаний — всё это передано во входе.\n\n"
    "Для каждой компании прими решение:\n"
    "- \"assign\" — данные ясно указывают на одну группу: тот же ИНН директора "
    "(не просто похожее ФИО — это может быть однофамилец), тот же адрес/регион, "
    "явное совпадение в учредителях или связанных компаниях.\n"
    "- \"needs_review\" — основания неоднозначны: директор может быть номинальным "
    "(на многих компаниях сразу), оба кандидата равно вероятны, данных мало. "
    "В этом случае group = null, но rationale должен объяснить, что именно неясно, "
    "чтобы человек быстро проверил вручную.\n\n"
    "Верни СТРОГО JSON-объект без пояснений вокруг:\n"
    '{"decisions": [\n'
    '  {"inn": "10-значный ИНН из входа", "decision": "assign|needs_review", '
    '"group": "точное название группы из входа или null", '
    '"confidence": "high|medium|low", '
    '"rationale": "1-2 предложения, только на основе входных данных"}\n'
    "]}"
)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".backup"))
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def load_source() -> tuple[Path, list[dict], list[str], bool]:
    if ENRICHED_CSV.exists():
        return ENRICHED_CSV, read_csv(ENRICHED_CSV), ENRICHED_FIELDS, True
    if not GROUPS_CSV.exists():
        raise SystemExit(f"Не найден ни {ENRICHED_CSV.name}, ни {GROUPS_CSV.name}")
    print(f"({ENRICHED_CSV.name} не найден — работаю с {GROUPS_CSV.name}: "
          f"можно разобрать только конфликты в уже назначенных группах, "
          f"новые группы назначать не из чего — нет ненагруппированных строк)")
    return GROUPS_CSV, read_csv(GROUPS_CSV), GROUPS_FIELDS, False


def dedupe_conflict_comment(comment: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """Схлопывает повторяющиеся 'Конфликт групп по директору (...): ...' в
    уникальные записи и возвращает (остаток_комментария_без_них, список_конфликтов)."""
    seen: dict[str, tuple[str, ...]] = {}
    for director, groups_str in CONFLICT_RE.findall(comment or ""):
        candidates = tuple(sorted({g.strip() for g in groups_str.split(",") if g.strip()}))
        seen[director.strip()] = candidates
    remainder = CONFLICT_RE.sub("", comment or "")
    remainder = re.sub(r"[;\s]{2,}", " ", remainder).strip(" ;.")
    return remainder, list(seen.items())


def build_roster(rows: list[dict], exclude_inns: set[str] = frozenset()) -> dict[str, list[dict]]:
    """Строит справочник «известные группы -> их компании» ТОЛЬКО из надёжных
    строк. exclude_inns обязателен для конфликтных/безгруппных компаний —
    иначе компания увидит собственную (спорную) текущую группу в этом же
    справочнике как «известный факт» и просто подтвердит её же, без реального
    сопоставления по директору/адресу. Это не гипотетический риск: первый
    прогон именно так и произошёл — 17/29 решений оказались круговыми
    ссылками на самих себя вида «уже указана в группе X»."""
    roster: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        inn = (r.get("ИНН") or "").strip()
        if inn and inn in exclude_inns:
            continue
        g = (r.get("Группа_Компаний") or "").strip()
        if g:
            roster[g].append({
                "name": (r.get("Юр_Лицо") or "").strip(),
                "address": (r.get("Контакты_ListOrg") or "").strip()[:120],
            })
    return roster


@dataclass
class Target:
    row: dict
    reason: str  # "conflict" | "no_group"
    base_comment: str = ""  # комментарий без дублей конфликт-текста, для сборки финальной строки
    conflict_candidates: list[str] = field(default_factory=list)
    conflict_director: str = ""


def find_targets(rows: list[dict]) -> list[Target]:
    """Определяет, какие строки нуждаются в решении ИИ. Дедупликация раздутого
    конфликт-текста считается здесь, но НЕ записывается в row немедленно —
    только apply_decisions() переписывает Комментарий, и только для строк,
    которые реально получили решение. Так при сбое батча (лимит API, сеть)
    маркер конфликта не теряется без замены — он просто остаётся как есть
    до следующего запуска."""
    targets: list[Target] = []
    for r in rows:
        if (r.get("Исключить") or "").strip():
            continue
        remainder, conflicts = dedupe_conflict_comment(r.get("Комментарий", ""))
        group = (r.get("Группа_Компаний") or "").strip()
        if conflicts:
            director, candidates = conflicts[0]
            targets.append(Target(row=r, reason="conflict", base_comment=remainder,
                                   conflict_candidates=list(candidates),
                                   conflict_director=director))
        elif not group:
            prior = (r.get("Комментарий") or "").strip()
            base = AI_NOTE_RE.sub("", prior).strip()
            targets.append(Target(row=r, reason="no_group", base_comment=base))
    return targets


def company_entry(t: Target) -> dict:
    r = t.row
    entry = {
        "inn": (r.get("ИНН") or "").strip(),
        "name": (r.get("Юр_Лицо") or "").strip(),
        "reason": t.reason,
        "director": (r.get("Директор_ListOrg") or "").strip(),
        "director_inn": (r.get("Директор_ИНН_ФЛ") or "").strip(),
        "address": (r.get("Контакты_ListOrg") or "").strip(),
        "founders": (r.get("Учредители_JSON") or "").strip(),
        "related_companies": (r.get("Связанные_Компании_JSON") or "").strip(),
    }
    if t.reason == "conflict":
        entry["conflict_director"] = t.conflict_director
        entry["candidate_groups"] = t.conflict_candidates
    return entry


def build_roster_summary(roster: dict[str, list[dict]], max_members: int = 5) -> dict:
    return {g: [m["name"] for m in members[:max_members]] for g, members in roster.items()}


def build_batch_user_content(batch: list[Target], roster: dict[str, list[dict]]) -> str:
    return json.dumps({
        "known_groups": build_roster_summary(roster),
        "companies": [company_entry(t) for t in batch],
    }, ensure_ascii=False, indent=2)


def call_openai(client, model: str, batch: list[Target], roster: dict[str, list[dict]]) -> list[dict]:
    user_content = build_batch_user_content(batch, roster)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("Пустой ответ от OpenAI")
    data = json.loads(content)
    return data.get("decisions", [])


def apply_decisions(targets_by_inn: dict[str, Target], decisions: list[dict]) -> tuple[int, int]:
    """Переписывает Комментарий только для строк с реальным решением от модели —
    используя t.base_comment (уже без дублей конфликт-текста), а не сырой
    r['Комментарий']. Строки без решения (сбой батча, модель их пропустила)
    остаются нетронутыми — конфликт-маркер сохраняется для следующего запуска."""
    assigned = reviewed = 0
    for d in decisions:
        inn = str(d.get("inn", "")).strip()
        t = targets_by_inn.get(inn)
        if not t:
            continue
        r = t.row
        rationale = (d.get("rationale") or "").strip()
        base = t.base_comment
        if d.get("decision") == "assign" and d.get("group"):
            r["Группа_Компаний"] = d["group"]
            note = f"[ИИ, {d.get('confidence', '?')}]: {rationale}" if rationale else "[ИИ]: группа назначена"
            r["Комментарий"] = (base + " " + note).strip() if base else note
            assigned += 1
        else:
            note = f"Требует проверки (ИИ): {rationale}" if rationale else "Требует проверки (ИИ)"
            r["Комментарий"] = (base + " " + note).strip() if base else note
            reviewed += 1
    return assigned, reviewed


def main() -> None:
    parser = argparse.ArgumentParser(description="ИИ-разбор конфликтов и недостающих групп компаний")
    parser.add_argument("--dry-run", action="store_true", help="Показать план без вызовов API")
    parser.add_argument("--limit", type=int, default=0, help="Максимум компаний к обработке (0 = все)")
    parser.add_argument("--batch-size", type=int, default=8, help="Компаний в одном запросе к модели")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o"))
    args = parser.parse_args()

    source_path, rows, fieldnames, is_enriched = load_source()
    print(f"Источник: {source_path.name}, строк: {len(rows)}")

    targets = find_targets(rows)
    target_inns = {(t.row.get("ИНН") or "").strip() for t in targets} - {""}
    roster = build_roster(rows, exclude_inns=target_inns)

    conflicts = [t for t in targets if t.reason == "conflict"]
    no_group = [t for t in targets if t.reason == "no_group"]
    print(f"Известных групп: {len(roster)}")
    print(f"Конфликтов по директору: {len(conflicts)}")
    print(f"Без группы: {len(no_group)}")

    if args.limit:
        targets = targets[:args.limit]

    if args.dry_run:
        batch = targets[:args.batch_size]
        if batch:
            print(f"\n[DRY RUN] Пример запроса (первая пачка из {len(batch)}):\n")
            print(build_batch_user_content(batch, roster))
        n_batches = (len(targets) + args.batch_size - 1) // args.batch_size
        print(f"\nВсего к обработке: {len(targets)} компаний, {n_batches} запрос(ов) к модели.")
        print("Дублирующийся конфликт-текст очищен только в памяти — "
              "запустите без --dry-run, чтобы сохранить очистку на диск.")
        return

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("\nOPENAI_API_KEY не задан в .env — добавьте ключ "
              "(тот же, что используется для AI Quota Competitor Monitor).")
        return

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    targets_by_inn = {
        (t.row.get("ИНН") or "").strip(): t for t in targets if (t.row.get("ИНН") or "").strip()
    }

    assigned_total = reviewed_total = 0
    batches = [targets[i:i + args.batch_size] for i in range(0, len(targets), args.batch_size)]
    for i, batch in enumerate(batches, 1):
        print(f"[{i}/{len(batches)}] пачка из {len(batch)}...", end=" ", flush=True)
        try:
            decisions = call_openai(client, args.model, batch, roster)
        except Exception as exc:  # сеть/лимиты/невалидный JSON — не роняем весь прогон
            print(f"✗ ошибка: {exc}")
            continue
        a, rv = apply_decisions(targets_by_inn, decisions)
        assigned_total += a
        reviewed_total += rv
        print(f"✓ назначено: {a}, на проверку: {rv}")
        time.sleep(0.5)

    save_csv(source_path, rows, fieldnames)
    if is_enriched:
        active = [r for r in rows if (r.get("Группа_Компаний") or "").strip() and not (r.get("Исключить") or "").strip()]
        save_csv(GROUPS_CSV, active, GROUPS_FIELDS)
        print(f"\nСохранено: {source_path.name}, {GROUPS_CSV.name} (+ .backup обоих)")
    else:
        print(f"\nСохранено: {source_path.name} (+ .backup)")

    print(f"ИТОГО: назначено ИИ: {assigned_total}, отправлено на ручную проверку: {reviewed_total}")
    print("Дальше: python3 scripts/export_company_groups_for_manual_edit.py — "
          "покажет только то, что реально требует вашего решения.")


if __name__ == "__main__":
    main()

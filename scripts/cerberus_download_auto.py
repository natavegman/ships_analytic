#!/usr/bin/env python3
"""
Автоматическая выгрузка реестра Цербер (cerberus.vetrf.ru) через браузер.

Запуск (один раз или по cron):
  pip install playwright
  playwright install chromium
  python3 scripts/cerberus_download_auto.py

Скачивает XLS в data/cerberus_export_latest.xlsx и при наличии pandas/openpyxl
сразу вызывает разбор и слияние (fetch_cerberus_export).
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SAVE_PATH = DATA_DIR / "cerberus_export_latest.xlsx"
CERBERUS_URL = "https://cerberus.vetrf.ru/cerberus/certified/pub"

# Браузеры Playwright в проекте (чтобы не зависеть от кэша IDE)
import os
PLAYWRIGHT_BROWSERS = BASE_DIR / ".playwright-browsers"
if PLAYWRIGHT_BROWSERS.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_BROWSERS)

# Таймауты (секунды)
PAGE_LOAD_TIMEOUT = 60_000
CLICK_TIMEOUT = 15_000
REPORT_WAIT = 180  # ждём готовности отчёта до 3 минут


def _chromium_executable() -> str | None:
    """Путь к Chromium в проекте (arm64), чтобы обойти выбор headless_shell x64."""
    exe = (
        PLAYWRIGHT_BROWSERS
        / "chromium-1208/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    )
    return str(exe) if exe.exists() else None


def run_browser_download(headless: bool = True) -> Path | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Установите Playwright: pip install playwright && playwright install chromium")
        return None

    with sync_playwright() as p:
        launch_opts = {"headless": headless}
        exe = _chromium_executable()
        if exe:
            launch_opts["executable_path"] = exe
        browser = p.chromium.launch(**launch_opts)
        context = browser.new_context(
            locale="ru-RU",
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        # Куда сохраняем отчёт
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        downloaded_path = None

        def handle_download(download):
            nonlocal downloaded_path
            try:
                download.save_as(SAVE_PATH)
                downloaded_path = SAVE_PATH
            except Exception as e:
                print(f"Ошибка сохранения: {e}")

        page.on("download", handle_download)

        try:
            page.goto(CERBERUS_URL, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
        except Exception as e:
            print(f"Ошибка загрузки страницы: {e}")
            browser.close()
            return None

        # Ждём появления контента (SPA)
        page.wait_for_timeout(3000)

        # Фильтр "Тип продукции" → "Рыба и морепродукты"
        try:
            page.get_by_text("Тип продукции", exact=False).first.wait_for(state="visible", timeout=CLICK_TIMEOUT)
            page.wait_for_timeout(500)
            # Выбор по value (опция в селекте может быть не видна до открытия)
            product_type_value = "0697e6d8-053d-11e1-99b4-d8d385fbc9e8"
            select_el = page.locator("select").filter(has=page.locator(f'option[value="{product_type_value}"]')).first
            select_el.select_option(value=product_type_value)
            page.wait_for_timeout(500)
        except Exception as e:
            print(f"Фильтр 'Тип продукции' (возможно, уже выбран или изменилась вёрстка): {e}")

        # Кнопка "Поиск" (может быть button, input или span внутри кнопки)
        try:
            for candidate in [
                page.get_by_role("button", name="Поиск"),
                page.locator("input[type='submit']").filter(has_text="Поиск"),
                page.locator("button", has_text="Поиск"),
                page.get_by_text("Поиск"),
            ]:
                if candidate.count() > 0:
                    candidate.first.scroll_into_view_if_needed()
                    candidate.first.click(timeout=CLICK_TIMEOUT)
                    break
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"Кнопка Поиск: {e}")

        # Открыть меню экспорта (пункт отчёта часто в скрытом dropdown)
        for menu_text in ["Экспорт", "Выгрузка", "Действия", "Отчет", "Отчёт"]:
            try:
                page.get_by_text(menu_text, exact=False).first.click(timeout=2000)
                page.wait_for_timeout(800)
                break
            except Exception:
                continue

        # "Сформировать новый отчет в формате xls" (пункт в dropdown — клик через JS если скрыт)
        try:
            link = page.get_by_text("Сформировать новый отчет в формате xls", exact=False).first
            link.wait_for(state="attached", timeout=CLICK_TIMEOUT)
            try:
                link.click(timeout=3000)
            except Exception:
                link.evaluate("el => el.click()")
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"Кнопка отчёта не найдена (возможно, нет результатов поиска): {e}")
            browser.close()
            return None

        # В модальном окне: количество записей (например 10000), потом "Сформировать"
        page.wait_for_timeout(1500)  # дать модалке открыться
        try:
            # Поле количества записей (в модалке часто type=number или пустой input)
            for sel in ['input[type="number"]', 'input[name*="count"]', 'input[placeholder*="записей"]', 'input[placeholder*="записи"]']:
                inp = page.locator(sel).first
                if inp.count() > 0 and inp.is_visible():
                    inp.fill("10000")
                    page.wait_for_timeout(300)
                    break
            # Кнопка "Сформировать" в модалке (ищем в overlay/dialog, чтобы не попасть в пункт меню)
            modal = page.locator("[role='dialog'], .modal, .dialog, [class*='modal'], [class*='dialog'], .ant-modal")
            if modal.count() > 0:
                form_btn = modal.first.get_by_text("Сформировать", exact=False).first
            else:
                form_btn = page.get_by_text("Сформировать", exact=False).last  # последний = обычно в модалке
            form_btn.wait_for(state="attached", timeout=CLICK_TIMEOUT)
            form_btn.click(force=True, timeout=CLICK_TIMEOUT)
        except Exception as e:
            print(f"Модальное окно отчёта: {e}")

        # Ждём появления скачивания (авто) или ссылки на отчёт
        print("Ожидание отчёта (до 3 мин)...")
        for i in range(REPORT_WAIT):
            page.wait_for_timeout(1000)
            if downloaded_path and SAVE_PATH.exists():
                break
            # Ссылка на скачивание (разные варианты на сайте)
            for selector in ['a[href*="reports/download"]', 'a[href*="download"]', 'a:has-text("Скачать")', 'a:has-text("скачать")']:
                dl_link = page.locator(selector).first
                if dl_link.count() > 0 and dl_link.is_visible():
                    try:
                        with page.expect_download(timeout=15_000) as download_info:
                            dl_link.click()
                        download = download_info.value
                        download.save_as(SAVE_PATH)
                        downloaded_path = SAVE_PATH
                        break
                    except Exception:
                        pass
            if downloaded_path:
                break
            if not downloaded_path and (i + 1) % 30 == 0:
                print(f"  ... прошло {(i + 1) // 60} мин")

        browser.close()

        if SAVE_PATH.exists():
            print(f"Скачано: {SAVE_PATH}")
            return SAVE_PATH
        print("Файл отчёта не получен. Возможно, лимит времени или изменилась структура страницы.")
        return None


def main() -> None:
    headless = "--headed" not in sys.argv
    path = run_browser_download(headless=headless)
    if not path:
        sys.exit(1)

    # Разбор XLS и слияние с компаниями
    sys.path.insert(0, str(BASE_DIR / "scripts"))
    import fetch_cerberus_export as fe
    read_cerberus_xls = fe.read_cerberus_xls
    map_cerberus_columns = fe.map_cerberus_columns
    save_cerberus_csv = fe.save_cerberus_csv
    is_vessel_record = fe.is_vessel_record
    CERBERUS_CSV = fe.CERBERUS_CSV
    COMPANIES_CSV = fe.COMPANIES_CSV
    OUTPUT_WITH_EXPORT = fe.OUTPUT_WITH_EXPORT
    build_companies_with_export = fe.build_companies_with_export
    rows = read_cerberus_xls(path)
    if not rows:
        print("Не удалось прочитать XLS. Оставлен только скачанный файл.")
        return
    normalized = [map_cerberus_columns(r) for r in rows]
    save_cerberus_csv(normalized, CERBERUS_CSV)
    print(f"Записано в {CERBERUS_CSV}: {len(normalized)} записей")
    vessels = sum(1 for r in normalized if is_vessel_record(r.get("Вид_объекта", "")))
    print(f"Из них суда: {vessels}")
    if COMPANIES_CSV.exists():
        build_companies_with_export(CERBERUS_CSV, COMPANIES_CSV, OUTPUT_WITH_EXPORT)


if __name__ == "__main__":
    main()

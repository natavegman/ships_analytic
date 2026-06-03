"""
Парсинг сайтов конкурентов и реестров через Selenium (headless, macOS).
"""

from __future__ import annotations

import logging
import re
import shutil
from contextlib import contextmanager
from io import BytesIO
from typing import Generator
from urllib.parse import urljoin

import httpx
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

WAIT_SECONDS = 10
PDF_URL_PATTERN = re.compile(r"""https?://[^\s"'<>]+\.pdf""", re.I)


def _build_chrome_options() -> ChromeOptions:
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    options.page_load_strategy = "eager"
    return options


def _resolve_chromedriver() -> str | None:
    try:
        from webdriver_manager.chrome import ChromeDriverManager

        return ChromeDriverManager().install()
    except Exception as exc:
        logger.warning("webdriver-manager недоступен, используем chromedriver из PATH: %s", exc)
        return shutil.which("chromedriver")


@contextmanager
def _chrome_driver() -> Generator[webdriver.Chrome, None, None]:
    options = _build_chrome_options()
    driver_path = _resolve_chromedriver()
    service = ChromeService(executable_path=driver_path) if driver_path else ChromeService()

    driver = webdriver.Chrome(service=service, options=options)
    try:
        yield driver
    finally:
        driver.quit()


def _clean_text(raw: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&quot;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _wait_for_content(driver: webdriver.Chrome) -> None:
    wait = WebDriverWait(driver, WAIT_SECONDS)
    try:
        wait.until(
            lambda d: (
                d.find_elements(By.TAG_NAME, "table")
                or d.find_elements(By.CSS_SELECTOR, "embed[type='application/pdf'], iframe[src*='.pdf']")
                or (d.find_element(By.TAG_NAME, "body").text.strip())
            )
        )
    except TimeoutException:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def _find_embedded_pdf_urls(page_source: str, base_url: str) -> list[str]:
    found: list[str] = []
    for match in PDF_URL_PATTERN.findall(page_source):
        pdf_url = match.replace("&amp;", "&")
        if not pdf_url.startswith(("http://", "https://")):
            pdf_url = urljoin(base_url, pdf_url)
        if pdf_url not in found:
            found.append(pdf_url)

    for element in ("embed", "iframe", "a"):
        pattern = re.compile(
            rf"""<{element}[^>]+(?:src|href)=["']([^"']+\.pdf[^"']*)["']""",
            re.I,
        )
        for rel_url in pattern.findall(page_source):
            pdf_url = urljoin(base_url, rel_url.replace("&amp;", "&"))
            if pdf_url not in found:
                found.append(pdf_url)

    return found


def _extract_pdf_text(pdf_url: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Установите pypdf: pip install pypdf") from exc

    response = httpx.get(pdf_url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()

    reader = PdfReader(BytesIO(response.content))
    parts: list[str] = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _merge_page_and_pdf_text(visible_text: str, pdf_sections: list[tuple[str, str]]) -> str:
    sections: list[str] = []

    nav_noise = {"компания", "продукция", "новости", "контакты", "english", "дислокация"}
    visible_lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
    content_lines = [
        line
        for line in visible_lines
        if line.lower() not in nav_noise and "©" not in line and "права защищены" not in line.lower()
    ]
    if content_lines:
        sections.append("\n".join(content_lines))

    for pdf_url, pdf_text in pdf_sections:
        if pdf_text.strip():
            sections.append(f"=== Данные из PDF ({pdf_url}) ===\n{pdf_text}")

    return _clean_text("\n\n".join(sections))


def parse_competitor_data(url: str) -> str:
    """
    Загружает страницу конкурента или реестра и возвращает очищенный текст.
    """
    if not url or not url.strip():
        raise ValueError("URL не может быть пустым")

    normalized_url = url.strip()
    if not normalized_url.startswith(("http://", "https://")):
        normalized_url = f"https://{normalized_url}"

    try:
        with _chrome_driver() as driver:
            driver.get(normalized_url)
            _wait_for_content(driver)

            body = driver.find_element(By.TAG_NAME, "body")
            page_source = driver.page_source
            visible_text = body.text.strip()

            pdf_urls = _find_embedded_pdf_urls(page_source, normalized_url)
            pdf_sections: list[tuple[str, str]] = []
            for pdf_url in pdf_urls:
                try:
                    pdf_text = _extract_pdf_text(pdf_url)
                    if pdf_text:
                        pdf_sections.append((pdf_url, pdf_text))
                        logger.info("Извлечён PDF: %s (%d символов)", pdf_url, len(pdf_text))
                except Exception as exc:
                    logger.warning("Не удалось извлечь PDF %s: %s", pdf_url, exc)

            if pdf_sections:
                return _merge_page_and_pdf_text(visible_text, pdf_sections)

            if visible_text:
                return _clean_text(visible_text)
            return _clean_text(page_source)
    except WebDriverException as exc:
        logger.error("Selenium error for %s: %s", normalized_url, exc)
        raise RuntimeError(f"Не удалось загрузить страницу: {exc}") from exc

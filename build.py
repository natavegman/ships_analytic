#!/usr/bin/env python3
"""
Сборка QuotaCompetitorMonitor.app для macOS через PyInstaller.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
APP_NAME = "QuotaCompetitorMonitor"


def _collect_pyqt6_hidden_imports() -> list[str]:
    return [
        "PyQt6",
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "PyQt6.sip",
    ]


def _collect_selenium_hidden_imports() -> list[str]:
    return [
        "selenium",
        "selenium.webdriver",
        "selenium.webdriver.chrome.service",
        "selenium.webdriver.chrome.options",
        "selenium.webdriver.common.by",
        "selenium.webdriver.support",
        "selenium.webdriver.support.ui",
        "selenium.webdriver.support.expected_conditions",
        "webdriver_manager",
        "webdriver_manager.chrome",
    ]


def _collect_fastapi_hidden_imports() -> list[str]:
    return [
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "fastapi",
        "starlette",
        "openai",
        "httpx",
        "pydantic",
        "dotenv",
        "pypdf",
        "vesselservice",
        "enrichservice",
    ]


def build() -> int:
    try:
        import PyInstaller.__main__
    except ImportError:
        print("PyInstaller не установлен. Выполните: pip install pyinstaller", file=sys.stderr)
        return 1

    hidden_imports = (
        _collect_pyqt6_hidden_imports()
        + _collect_selenium_hidden_imports()
        + _collect_fastapi_hidden_imports()
    )

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name",
        APP_NAME,
        "--windowed",
        "--onefile",
        "--noconfirm",
        "--clean",
        str(ROOT / "gui.py"),
        "--add-data",
        f"{ROOT / 'main.py'}{Path.pathsep}.",
        "--add-data",
        f"{ROOT / 'openaiservice.py'}{Path.pathsep}.",
        "--add-data",
        f"{ROOT / 'parsingservice.py'}{Path.pathsep}.",
        "--add-data",
        f"{ROOT / 'vesselservice.py'}{Path.pathsep}.",
        "--add-data",
        f"{ROOT / 'enrichservice.py'}{Path.pathsep}.",
    ]

    data_bundle = [
        ROOT / "notion_import" / "vessels.csv",
        ROOT / "data" / "gfw_our_vessels.json",
    ]
    for data_file in data_bundle:
        if data_file.exists():
            rel_parent = data_file.parent.relative_to(ROOT)
            cmd.extend(["--add-data", f"{data_file}{Path.pathsep}{rel_parent}"])

    for module in hidden_imports:
        cmd.extend(["--hidden-import", module])

    env_example = ROOT / ".env.example"
    if env_example.exists():
        cmd.extend(["--add-data", f"{env_example}{Path.pathsep}."])

    print("Запуск PyInstaller:")
    print(" ", " ".join(cmd))

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    app_bundle = DIST_DIR / f"{APP_NAME}.app"
    onefile_binary = DIST_DIR / APP_NAME

    print("\nСборка завершена.")
    if app_bundle.exists():
        print(f"  macOS bundle: {app_bundle}")
    if onefile_binary.exists():
        print(f"  Исполняемый файл: {onefile_binary}")

    print(
        "\nПримечание: для Selenium нужен установленный Google Chrome "
        "и доступ к chromedriver (webdriver-manager скачает его при первом запуске)."
    )
    print("Задайте OPENAI_API_KEY в .env или переменных окружения перед запуском.")

    if BUILD_DIR.exists():
        print(f"\nВременные файлы сборки: {BUILD_DIR} (можно удалить вручную)")

    return 0


if __name__ == "__main__":
    raise SystemExit(build())

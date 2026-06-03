#!/usr/bin/env python3
"""
PyQt6 GUI для AI Quota Competitor Monitor (macOS style).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import threading
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QPalette, QColor
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_env = ROOT / ".env"
if _env.exists():
    load_dotenv(_env)

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")
API_PORT = int(os.getenv("API_PORT", "8000"))

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
PDF_EXTENSIONS = {".pdf"}


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Установите pypdf: pip install pypdf") from exc

    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _wait_for_api(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{API_BASE}/health", timeout=2.0)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def _start_api_server() -> None:
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=API_PORT, log_level="warning")


class ApiWorker(QThread):
    finished_ok = pyqtSignal(dict)
    finished_error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, task: str, payload: dict | None = None, file_path: str | None = None):
        super().__init__()
        self.task = task
        self.payload = payload or {}
        self.file_path = file_path

    def run(self) -> None:
        try:
            if self.task == "parse_and_analyze":
                self._parse_and_analyze()
            elif self.task == "analyze_file":
                self._analyze_file()
            else:
                self.finished_error.emit(f"Неизвестная задача: {self.task}")
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            try:
                detail = exc.response.json().get("detail", detail)
            except Exception:
                pass
            self.finished_error.emit(f"HTTP {exc.response.status_code}: {detail}")
        except httpx.HTTPError as exc:
            self.finished_error.emit(f"Сетевая ошибка: {exc}")
        except Exception as exc:
            self.finished_error.emit(str(exc))

    def _post_json(self, endpoint: str, data: dict) -> dict:
        with httpx.Client(base_url=API_BASE, timeout=120.0) as client:
            response = client.post(endpoint, json=data)
            response.raise_for_status()
            return response.json()

    def _parse_and_analyze(self) -> None:
        url = self.payload["url"]
        self.status.emit("Парсинг, анализ и обогащение из базы…")
        result = self._post_json("/monitor", {"url": url, "sync_notion": False})
        self.finished_ok.emit(result)

    def _analyze_file(self) -> None:
        path = Path(self.file_path or "")
        suffix = path.suffix.lower()

        if suffix in PDF_EXTENSIONS:
            self.status.emit("Извлечение текста из PDF…")
            text = _extract_pdf_text(path)
            if not text:
                self.finished_error.emit("PDF не содержит извлекаемого текста")
                return
            self.status.emit("Анализ PDF и обогащение из базы…")
            result = self._post_json(
                "/analyze-enrich",
                {"text": text[:120_000], "source_url": str(path), "sync_notion": False},
            )
            self.finished_ok.emit(result)
            return

        if suffix in IMAGE_EXTENSIONS:
            self.status.emit("Загрузка изображения…")
            mime_type, _ = mimetypes.guess_type(str(path))
            mime_type = mime_type or "image/png"
            image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            self.status.emit("Анализ изображения и обогащение…")
            analysis = self._post_json(
                "/analyzeimage",
                {"image_base64": image_b64, "mime_type": mime_type},
            )
            result = self._post_json(
                "/enrich",
                {"analysis": analysis, "source_url": str(path), "sync_notion": False},
            )
            self.finished_ok.emit(result)
            return

        self.finished_error.emit(f"Неподдерживаемый формат файла: {suffix}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._worker: ApiWorker | None = None
        self.setWindowTitle("AI Quota Competitor Monitor")
        self.setMinimumSize(1100, 720)
        self._apply_mac_style()
        self._build_ui()

    def _apply_mac_style(self) -> None:
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#f5f5f7"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#1d1d1f"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f5f5f7"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#1d1d1f"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#1d1d1f"))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#86868b"))
        self.setPalette(palette)

        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #f5f5f7;
                color: #1d1d1f;
            }
            QLineEdit, QTextEdit {
                background-color: #ffffff;
                color: #1d1d1f;
                border: 1px solid #d2d2d7;
                border-radius: 8px;
                padding: 8px;
                font-size: 14px;
                selection-background-color: #0071e3;
                selection-color: #ffffff;
            }
            QLineEdit::placeholder, QTextEdit[placeholderText] {
                color: #86868b;
            }
            QPushButton {
                background-color: #0071e3;
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 10px 16px;
                font-size: 14px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: #0077ed; }
            QPushButton:disabled {
                background-color: #a1a1a6;
                color: #ffffff;
            }
            QLabel { color: #1d1d1f; font-size: 13px; }
            QSplitter::handle { background-color: #d2d2d7; }
            """
        )

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("AI Quota Competitor Monitor")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        layout.addWidget(title)

        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "URL конкурента, реестра или диспетчерской "
            "(напр. https://okeanrybflot.ru/dislocation/)"
        )
        self.parse_btn = QPushButton("Спарсить сайт")
        self.parse_btn.clicked.connect(self._on_parse_clicked)
        url_row.addWidget(self.url_input, stretch=1)
        url_row.addWidget(self.parse_btn)
        layout.addLayout(url_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.upload_btn = QPushButton(
            "Загрузить PDF приказа / Скриншот GFW / Fishfacts / Marinetraffic"
        )
        self.upload_btn.clicked.connect(self._on_upload_clicked)
        left_layout.addWidget(self.upload_btn)
        left_layout.addStretch()
        splitter.addWidget(left_panel)

        self.report_view = QTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setPlaceholderText("JSON-отчёт появится здесь после анализа…")
        self.report_view.setStyleSheet(
            "QTextEdit { background-color: #ffffff; color: #1d1d1f; }"
        )
        mono = QFont("Menlo", 12)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.report_view.setFont(mono)
        splitter.addWidget(self.report_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, stretch=1)

        self.status_label = QLabel("Готово")
        self.status_label.setStyleSheet("color: #6e6e73;")
        layout.addWidget(self.status_label)

    def _set_busy(self, busy: bool) -> None:
        self.parse_btn.setEnabled(not busy)
        self.upload_btn.setEnabled(not busy)

    def _on_worker_finished_ok(self, result: dict) -> None:
        formatted = json.dumps(result, ensure_ascii=False, indent=2)
        self.report_view.setPlainText(formatted)
        self.status_label.setText("Анализ завершён")
        self._set_busy(False)
        self._worker = None

    def _on_worker_finished_error(self, message: str) -> None:
        self.status_label.setText("Ошибка")
        self._set_busy(False)
        self._worker = None
        QMessageBox.critical(self, "Ошибка", message)

    def _start_worker(self, task: str, payload: dict | None = None, file_path: str | None = None) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "Подождите", "Уже выполняется запрос…")
            return

        self._set_busy(True)
        self._worker = ApiWorker(task, payload, file_path)
        self._worker.status.connect(self.status_label.setText)
        self._worker.finished_ok.connect(self._on_worker_finished_ok)
        self._worker.finished_error.connect(self._on_worker_finished_error)
        self._worker.start()

    def _on_parse_clicked(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "URL", "Введите URL для парсинга")
            return
        self._start_worker("parse_and_analyze", {"url": url})

    def _on_upload_clicked(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл",
            str(ROOT),
            "Документы и изображения (*.pdf *.png *.jpg *.jpeg *.webp *.gif *.bmp);;All Files (*)",
        )
        if not file_path:
            return
        self._start_worker("analyze_file", file_path=file_path)


def main() -> None:
    server_thread = threading.Thread(target=_start_api_server, daemon=True)
    server_thread.start()

    if not _wait_for_api():
        print("Не удалось дождаться запуска FastAPI на", API_BASE, file=sys.stderr)

    app = QApplication(sys.argv)
    app.setApplicationName("QuotaCompetitorMonitor")

    light_palette = QPalette()
    light_palette.setColor(QPalette.ColorRole.Window, QColor("#f5f5f7"))
    light_palette.setColor(QPalette.ColorRole.WindowText, QColor("#1d1d1f"))
    light_palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    light_palette.setColor(QPalette.ColorRole.Text, QColor("#1d1d1f"))
    light_palette.setColor(QPalette.ColorRole.ButtonText, QColor("#1d1d1f"))
    light_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#86868b"))
    app.setPalette(light_palette)
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

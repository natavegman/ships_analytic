"""
FastAPI-бэкенд AI Quota Competitor Monitor.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from enrichservice import enrich_competitor_report, run_background_notion_pipeline
from openaiservice import analyze_text, router as analysis_router
from openaiservice import TextAnalysisRequest
from parsingservice import parse_competitor_data

_env = Path(__file__).resolve().parent / ".env"
if _env.exists():
    load_dotenv(_env)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

app = FastAPI(
    title="AI Quota Competitor Monitor",
    description="Анализ квот Росрыболовства и конкурентов",
    version="1.0.0",
)

app.include_router(analysis_router)


class ParseRequest(BaseModel):
    url: str = Field(..., min_length=1)


class ParseResponse(BaseModel):
    url: str
    text: str


class MonitorRequest(BaseModel):
    url: str = Field(..., min_length=1)
    sync_notion: bool = Field(default=False, description="Пересобрать notion_import и опционально push в Notion")


class AnalyzeEnrichRequest(BaseModel):
    text: str = Field(..., min_length=1)
    source_url: str | None = None
    sync_notion: bool = False


class EnrichRequest(BaseModel):
    analysis: dict
    source_url: str | None = None
    sync_notion: bool = False


class EnrichRequest(BaseModel):
    analysis: dict = Field(..., description="JSON-отчёт после /analyzetext или /analyzeimage")
    source_url: str | None = None
    sync_notion: bool = False


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/parse", response_model=ParseResponse)
async def parse_url(request: ParseRequest) -> ParseResponse:
    """Спарсить сайт конкурента или реестра через Selenium."""
    try:
        text = parse_competitor_data(request.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not text.strip():
        raise HTTPException(status_code=502, detail="Страница загружена, но текст не извлечён")

    return ParseResponse(url=request.url.strip(), text=text)


@app.post("/monitor")
async def monitor_competitor(request: MonitorRequest, background_tasks: BackgroundTasks) -> dict:
    """
    Полный цикл: парсинг сайта → AI-анализ → связка с группами/квотами/Цербер → БД → снимок JSON.
    """
    try:
        text = parse_competitor_data(request.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not text.strip():
        raise HTTPException(status_code=502, detail="Страница загружена, но текст не извлечён")

    analysis = await analyze_text(TextAnalysisRequest(text=text[:120_000]))
    enriched = enrich_competitor_report(analysis, source_url=request.url.strip(), persist_db=True)
    enriched["source_text_chars"] = len(text)
    enriched["source_text_preview"] = text[:1500]

    if request.sync_notion:
        background_tasks.add_task(run_background_notion_pipeline)
        enriched["notion_sync"] = "scheduled"
    else:
        enriched["notion_sync"] = "skipped"

    return enriched


@app.post("/analyze-enrich")
async def analyze_and_enrich(
    request: AnalyzeEnrichRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """AI-анализ текста/PDF с обогащением из реестров проекта."""
    analysis = await analyze_text(TextAnalysisRequest(text=request.text[:120_000]))
    enriched = enrich_competitor_report(
        analysis,
        source_url=request.source_url,
        persist_db=True,
    )
    enriched["source_text_chars"] = len(request.text)
    enriched["source_text_preview"] = request.text[:1500]

    if request.sync_notion:
        background_tasks.add_task(run_background_notion_pipeline)
        enriched["notion_sync"] = "scheduled"
    else:
        enriched["notion_sync"] = "skipped"
    return enriched


@app.post("/enrich")
async def enrich_analysis(request: EnrichRequest, background_tasks: BackgroundTasks) -> dict:
    """Обогатить готовый AI-отчёт данными проекта (группы, квоты, Цербер, БД)."""
    enriched = enrich_competitor_report(
        request.analysis,
        source_url=request.source_url,
        persist_db=True,
    )
    if request.sync_notion:
        background_tasks.add_task(run_background_notion_pipeline)
        enriched["notion_sync"] = "scheduled"
    else:
        enriched["notion_sync"] = "skipped"
    return enriched


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("main:app", host="127.0.0.1", port=port, reload=False)

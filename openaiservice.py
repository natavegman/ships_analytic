"""
AI-аналитика конкурентов: эндпоинты /analyzeimage и /analyzetext (AsyncOpenAI, gpt-4o).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel, Field

from vesselservice import enrich_analysis_with_imo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analysis"])

SYSTEM_PROMPT = (
    "Ты эксперт-аналитик рыбной отрасли РФ. Проанализируй ТОЛЬКО те данные, "
    "которые явно присутствуют во входном тексте или на скриншоте. "
    "ЗАПРЕЩЕНО придумывать, догадываться или подставлять типовые значения.\n\n"
    "Правила:\n"
    "- competitor_name — только если название компании прямо указано во входе.\n"
    "- target_species — только виды/объекты лова, упомянутые во входе.\n"
    "- fishing_basins — только бассейны/акватории из входа (моря, бассейны). "
    "Не заменяй «Охотское море» на «Дальневосточный», если это не написано.\n"
    "- estimated_quota_tons — только если во входе есть конкретный объём в тоннах; "
    "иначе null.\n"
    "- active_vessels — только конкретные названия судов из входа (без ИМО; ИМО добавит система). "
    "Не записывай описания флота («14 БМРТ») как название судна. "
    "Если судов поимённо нет — [].\n"
    "- vessel_dislocation — если во входе есть таблица диспетчерской/дислокации, "
    "массив объектов {\"name\", \"status\", \"location\", \"end_date\"} строго из источника; "
    "поле imo не заполняй — его добавит система. "
    "иначе [].\n"
    "- market_threat_score — оценка 0–10 только на основе фактов из входа; "
    "если данных мало — ставь ниже и объясни в strategic_summary.\n"
    "- strategic_summary — кратко, только подтверждённые факты; "
    "если чего-то нет во входе, явно напиши «не указано в источнике».\n"
    "- facts_from_source — массив из 3–7 дословных или почти дословных цитат/фактов из входа.\n"
    "- missing_data — массив полей, которых не хватает во входе "
    "(например: «объём квот в тоннах», «ИМО судов»).\n\n"
    "Верни ответ СТРОГО в формате JSON:\n"
    "{\n"
    '"competitor_name": "Название холдинга/компании или null",\n'
    '"target_species": ["объекты лова из источника"],\n'
    '"fishing_basins": ["бассейны/моря из источника"],\n'
    '"estimated_quota_tons": null,\n'
    '"active_vessels": ["названия судов из источника"],\n'
    '"vessel_dislocation": [{"name": "судно", "status": "промысел/ремонт/...", "location": "...", "end_date": "..."}],\n'
    '"market_threat_score": 0,\n'
    '"strategic_summary": "Выжимка только по фактам из источника",\n'
    '"facts_from_source": ["цитата или факт 1"],\n'
    '"missing_data": ["чего нет в источнике"]\n'
    "}"
)

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")


def _get_client() -> AsyncOpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY не задан. Добавьте ключ в переменные окружения или .env",
        )
    return AsyncOpenAI(api_key=api_key)


class TextAnalysisRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Текст приказа, реестра или сайта")


class ImageAnalysisRequest(BaseModel):
    image_base64: str = Field(..., min_length=1, description="Изображение в base64")
    mime_type: str = Field(default="image/png", description="MIME-тип изображения")


def _parse_json_response(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.error("Модель вернула невалидный JSON: %s", content[:500])
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI вернул некорректный JSON: {exc}",
        ) from exc


async def _call_openai(messages: list[dict[str, Any]]) -> dict[str, Any]:
    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
        )
    except RateLimitError as exc:
        logger.warning("OpenAI rate limit: %s", exc)
        raise HTTPException(status_code=429, detail="Превышен лимит запросов OpenAI") from exc
    except APIConnectionError as exc:
        logger.error("OpenAI connection error: %s", exc)
        raise HTTPException(status_code=503, detail="Не удалось подключиться к OpenAI API") from exc
    except APIStatusError as exc:
        logger.error("OpenAI API error %s: %s", exc.status_code, exc.message)
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка OpenAI API ({exc.status_code}): {exc.message}",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected OpenAI error")
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка при вызове OpenAI: {exc}") from exc

    choice = response.choices[0].message.content
    if not choice:
        raise HTTPException(status_code=502, detail="OpenAI вернул пустой ответ")
    return _parse_json_response(choice)


@router.post("/analyzetext")
async def analyze_text(request: TextAnalysisRequest) -> dict[str, Any]:
    """Анализ текстового контента (приказ, реестр, спарсенный сайт)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Проанализируй следующий текст и верни JSON-отчёт:\n\n{request.text}",
        },
    ]
    return enrich_analysis_with_imo(await _call_openai(messages))


@router.post("/analyzeimage")
async def analyze_image(request: ImageAnalysisRequest) -> dict[str, Any]:
    """Анализ скриншота (GFW, Fishfacts, Marinetraffic, сайт конкурента)."""
    try:
        base64.b64decode(request.image_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Некорректные данные image_base64") from exc

    data_url = f"data:{request.mime_type};base64,{request.image_base64}"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Проанализируй скриншот и верни JSON-отчёт о конкуренте.",
                },
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    return enrich_analysis_with_imo(await _call_openai(messages))

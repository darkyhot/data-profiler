"""LLM-клиент закрытого контура — переключаемый ТРАНСПОРТ подключения.

method (транспорт, не модель):
  "http"     — requests → {base_url}/chat/completions, Bearer-токен
               (OpenAI-совместимый; годится и для шлюза, и для deepseek)
  "gigachat" — langchain_gigachat.GigaChat (base_url + access_token)
  None       — LLM отключён

Модель (Qwen3.5-397b, glm-5.1, deepseek-chat, …) задаётся отдельно в cfg.model и
работает в любом транспорте. URL и токен берутся из переменных окружения (по
умолчанию GIGACHAT_API_URL / JPY_API_TOKEN), либо задаются в конфиге напрямую.
Всё остальное (профиль/ресэмплинг) работает и без LLM — тогда синтезатор
использует детерминированный фолбэк-фейкер.
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests

from .config import LLMConfig

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.method = (cfg.method or "").lower() or None
        self.base_url = cfg.base_url or os.getenv(cfg.base_url_env, "")
        self.token = cfg.token or os.getenv(cfg.token_env, "")
        self._giga = None
        if self.method == "gigachat":
            self._init_gigachat()

    @property
    def enabled(self) -> bool:
        return self.method is not None

    def _init_gigachat(self) -> None:
        try:
            from langchain_gigachat.chat_models import GigaChat
        except ImportError as exc:  # noqa: BLE001
            raise LLMError(
                "method='gigachat' требует пакет langchain-gigachat. "
                "Установите его или используйте method='http'."
            ) from exc
        self._giga = GigaChat(base_url=self.base_url, access_token=self.token, model=self.cfg.model)

    # ── публичный API ────────────────────────────────────────────────────────
    def complete(self, system: str, user: str) -> str:
        if not self.enabled:
            raise LLMError("LLM отключён (method=None).")
        if self.method == "gigachat":
            return self._complete_gigachat(system, user)
        return self._complete_http(system, user)

    def complete_json(self, system: str, user: str) -> dict:
        """Ответ, распарсенный как JSON. Терпим к обёрткам ```json и мусору."""
        raw = self.complete(system + "\nОтвечай СТРОГО валидным JSON без пояснений.", user)
        return _extract_json(raw)

    # ── реализации ───────────────────────────────────────────────────────────
    def _complete_gigachat(self, system: str, user: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage
        msgs = [SystemMessage(content=system), HumanMessage(content=user)]
        return self._giga.invoke(msgs).content

    def _complete_http(self, system: str, user: str) -> str:
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        data = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "n": 1,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        resp = requests.post(url, headers=headers, json=data, timeout=self.cfg.timeout_s)
        if not resp.ok:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:  # noqa: BLE001
            raise LLMError(f"Неожиданный ответ LLM: {json.dumps(payload)[:500]}") from exc


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    # срез от первой { или [ до парной скобки
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start >= 0:
        text = text[start:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Не удалось распарсить JSON из ответа LLM: {text[:300]}") from exc

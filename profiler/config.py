"""Конфиг прогона — собирается из ОДНОЙ ячейки юпитер-тетрадки.

Ячейка задаёт параметры (подключение, список таблиц, корр-группы, объёмы) и
вызывает profiler.run(locals()). Здесь эти переменные валидируются и
превращаются в типизированный RunConfig. Никакой логики — только конфиг.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMConfig:
    # method — ТРАНСПОРТ подключения (не модель; модель задаётся отдельно в model):
    #   "http"     — requests → {base_url}/chat/completions, Bearer-токен
    #                (OpenAI-совместимый; годится и для шлюза, и для deepseek)
    #   "gigachat" — langchain_gigachat.GigaChat (base_url + access_token)
    #   None       — без LLM (только детерминированный фолбэк-фейкер)
    # Любую модель (Qwen3.5-397b, glm-5.1, deepseek-chat, …) можно гонять в обоих
    # транспортах — задаётся полем model.
    method: str | None = "http"
    model: str = "Qwen3.5-397b"
    base_url_env: str = "GIGACHAT_API_URL"   # переменная окружения с URL шлюза
    token_env: str = "JPY_API_TOKEN"         # переменная окружения с токеном
    base_url: str | None = None              # можно задать напрямую (в обход env)
    token: str | None = None
    temperature: float = 0.2
    max_tokens: int = 8000
    timeout_s: int = 120


@dataclass
class RunConfig:
    db_url: str
    tables: list[str]                              # ["schema.table", ...]
    correlated_groups: list[list[str]] = field(default_factory=list)
    max_categories: int = 300                      # уник. <= этого → перечислить ВСЕ значения
    sample_rows_profile: int = 100_000             # сколько строк тянуть в pandas для профиля
    synth_rows: int = 1000                         # сколько синтетических строк генерить
    llm_pool_size: int = 60                        # размер LLM-пула фейков на колонку/ключ
    output_dir: Path = Path("./output")
    llm: LLMConfig = field(default_factory=LLMConfig)
    seed: int = 42                                 # детерминизм ресэмплинга/фейкера

    def __post_init__(self) -> None:
        if not self.tables:
            raise ValueError("TABLES пуст — задайте список 'schema.table'.")
        for t in self.tables:
            if "." not in t:
                raise ValueError(f"Таблица '{t}' должна быть в формате schema.table")
        self.output_dir = Path(self.output_dir)


def _build_db_url(ns: dict) -> str:
    """DB_URL целиком, либо собрать из частей. Пароль/порт — опциональны."""
    if ns.get("DB_URL"):
        return str(ns["DB_URL"])
    user = ns.get("DB_USER", "")
    pwd = ns.get("DB_PASSWORD", "")
    host = ns.get("DB_HOST", "localhost")
    port = ns.get("DB_PORT", 5432)
    name = ns.get("DB_NAME", "")
    auth = user + (f":{pwd}" if pwd else "")
    hostpart = f"{host}:{port}" if port else host
    return f"postgresql+psycopg2://{auth}@{hostpart}/{name}"


def from_namespace(ns: dict) -> RunConfig:
    """Построить RunConfig из словаря переменных ячейки (обычно locals())."""
    llm_raw = ns.get("LLM") or {}
    llm = LLMConfig(**llm_raw) if isinstance(llm_raw, dict) else llm_raw
    return RunConfig(
        db_url=_build_db_url(ns),
        tables=list(ns.get("TABLES", [])),
        correlated_groups=[list(g) for g in ns.get("CORRELATED_GROUPS", [])],
        max_categories=int(ns.get("MAX_CATEGORIES", 300)),
        sample_rows_profile=int(ns.get("SAMPLE_ROWS_PROFILE", 100_000)),
        synth_rows=int(ns.get("SYNTH_ROWS", 1000)),
        llm_pool_size=int(ns.get("LLM_POOL_SIZE", 60)),
        output_dir=Path(ns.get("OUTPUT_DIR", "./output")),
        llm=llm,
        seed=int(ns.get("SEED", 42)),
    )

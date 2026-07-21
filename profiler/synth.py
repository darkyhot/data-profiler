"""Синтез сэмпла таблицы: правдоподобные, но ТЕСТОВЫЕ строки.

Принципы (см. требования пользователя):
- Кардинальность и доля null сохраняются по профилю → агент тестирует SQL как
  на реальных данных (уникальность, IS NULL, GROUP BY ведут себя так же).
- Категории (тип задачи, статус …) — РЕАЛЬНЫЕ значения, ресэмпл из совместного
  распределения сэмпла (это не чувствительные данные, их сохраняем).
- Корр-группы ([task_type, task_subtype, task_questionary]) генерятся ВМЕСТЕ:
  «якорь» (категориальные члены) ресэмплится реальным кортежем, а зависимый
  free-text (опросник) — из фейкового пула, ПРИВЯЗАННОГО к якорю → значения не
  перемешиваются между подтипами.
- Чувствительные (ИНН/ФИО/деньги) — полностью фейковые (LLM-пул → фолбэк-фейкер),
  «не похожи на настоящие».

Стратегия масштаба: LLM генерит ПУЛЫ значений (десятки), а строки (тысячи)
набираются ресэмплингом из пулов — так 1000+ строк не упираются в LLM.
"""

from __future__ import annotations

import logging
import random

import pandas as pd

from .config import RunConfig
from .faker import Faker
from .llm import LLMClient, LLMError
from .profile import ColumnProfile, TableProfile

logger = logging.getLogger(__name__)

_NUM = ("int", "numeric", "decimal", "double", "real", "float", "smallint", "bigint", "money")
_INT = ("int", "smallint", "bigint")
_DATE = ("date", "timestamp", "time")
_BOOL = ("bool",)


def _is(dtype: str, fam: tuple) -> bool:
    d = (dtype or "").lower()
    return any(t in d for t in fam)


class Synthesizer:
    def __init__(self, cfg: RunConfig, llm: LLMClient | None):
        self.cfg = cfg
        self.llm = llm if (llm and llm.enabled) else None
        self.faker = Faker(cfg.seed)
        self.r = random.Random(cfg.seed)

    # ── публичный вход ───────────────────────────────────────────────────────
    def synth_table(self, profile: TableProfile, df: pd.DataFrame,
                    groups: list[list[str]]) -> pd.DataFrame:
        n = self.cfg.synth_rows
        by_name = {c.name: c for c in profile.columns}
        cols_order = [c.name for c in profile.columns]
        out: dict[str, list] = {}

        # 1) корр-группы (присутствующие в таблице целиком) — генерятся вместе
        handled: set[str] = set()
        for g in groups:
            present = [c for c in g if c in by_name and c in df.columns]
            if len(present) < 2:
                continue
            self._synth_group(present, by_name, df, n, out)
            handled.update(present)

        # 2) остальные колонки — независимо
        for name in cols_order:
            if name in handled:
                continue
            out[name] = self._synth_column(by_name[name], df, n)

        frame = pd.DataFrame(out)
        # 3) null-маски (кроме PK-гипотезы) по доле not_null_perc
        for name in cols_order:
            cp = by_name[name]
            if not cp.is_pk_hypothesis:
                self._apply_nulls(frame, name, cp.not_null_perc, n)
        return frame.reindex(columns=cols_order)

    # ── корр-группы ──────────────────────────────────────────────────────────
    def _synth_group(self, cols: list[str], by_name: dict, df: pd.DataFrame,
                     n: int, out: dict) -> None:
        """Якорь = категориальные/низкоуникальные члены; зависимые = free-text/
        чувствительные. Ресэмплим реальные кортежи якоря, зависимые тянем из
        пула, привязанного к якорю."""
        anchors = [c for c in cols if self._is_anchor(by_name[c])]
        deps = [c for c in cols if c not in anchors]
        if not anchors:                       # нет якоря — трактуем все как независимые
            anchors = cols[:1]
            deps = cols[1:]

        real = df[cols].dropna(how="all")
        if real.empty:
            for c in cols:
                out[c] = self._synth_column(by_name[c], df, n)
            return

        # ресэмпл кортежей ЯКОРЯ из реального совместного распределения
        idx = real[anchors].dropna().sample(n=n, replace=True, random_state=self.cfg.seed)
        for a in anchors:
            out[a] = idx[a].astype(object).tolist()

        # зависимые: пул фейков на каждый уникальный якорь → назначение по строке
        anchor_keys = [self._key(row) for row in idx[anchors].itertuples(index=False)]
        for dep in deps:
            pools = self._freetext_pools_by_anchor(dep, by_name[dep], anchors, df)
            vals = []
            for k in anchor_keys:
                pool = pools.get(k) or pools.get("__default__") or ["SYN"]
                vals.append(self.r.choice(pool))
            out[dep] = vals

    def _is_anchor(self, cp: ColumnProfile) -> bool:
        # якорь — то, что сохраняем реальным и что определяет зависимые
        return (cp.categories is not None) or cp.semantic_class in ("enum_like", "flag", "join_key")

    def _key(self, row) -> str:
        return " | ".join("" if pd.isna(v) else str(v) for v in row)

    def _freetext_pools_by_anchor(self, dep: str, cp: ColumnProfile, anchors: list[str],
                                  df: pd.DataFrame) -> dict[str, list]:
        """Для каждого реального якоря — пул фейковых значений зависимого поля.
        LLM (если включён) переписывает реальные примеры в фейковые «в стиле
        подтипа»; иначе фолбэк-фейкер/шаблон. Реальные примеры — только контекст,
        наружу уходят лишь фейки."""
        sub = df[anchors + [dep]].dropna(subset=[dep])
        examples: dict[str, list[str]] = {}
        for row in sub.itertuples(index=False):
            key = self._key(row[:-1])
            examples.setdefault(key, [])
            if len(examples[key]) < 5 and str(row[-1]).strip():
                examples[key].append(str(row[-1]).strip())
        if not examples:
            return {"__default__": self._fallback_pool(cp)}

        # LLM переписывает примеры в фейки, если поле текстовое/не «жёсткая» PII
        if self.llm and (cp.semantic_class == "free_text" or not cp.is_sensitive):
            try:
                return self._llm_pools(dep, examples)
            except LLMError as exc:  # noqa: BLE001
                logger.warning("LLM-пул для %s не сгенерирован: %s — фолбэк", dep, exc)

        # фолбэк: фейк по kind, либо шаблон на основе якоря
        out: dict[str, list] = {}
        for key in examples:
            if cp.is_sensitive and cp.sensitive_kind:
                out[key] = self.faker.pool(cp.sensitive_kind, 8)
            else:
                out[key] = [f"{key} — тестовый вариант {i+1}" for i in range(5)]
        return out

    def _llm_pools(self, dep: str, examples: dict[str, list[str]]) -> dict[str, list]:
        """Батч-запрос: {якорь: [реальные примеры]} → {якорь: [фейковые]}."""
        keys = list(examples.keys())
        result: dict[str, list] = {}
        system = (
            "Ты генерируешь СИНТЕТИЧЕСКИЕ значения одного поля для тестовой БД. "
            "Для каждого ключа верни массив выдуманных значений, ПОХОЖИХ по смыслу и "
            "формату на приведённые реальные примеры этого ключа, но полностью "
            "вымышленных (никаких реальных персональных/финансовых данных). "
            'Ответ строго JSON вида {"ключ": ["значение", ...], ...} с теми же ключами.'
        )
        per = max(3, self.cfg.llm_pool_size // max(1, min(len(keys), 20)))
        for i in range(0, len(keys), 20):        # батчами по 20 якорей
            chunk = keys[i:i + 20]
            payload = {k: examples[k][:5] for k in chunk}
            user = (f"Поле: {dep}\nСгенерируй по {per} значений на ключ.\n"
                    f"Реальные примеры по ключам (JSON):\n"
                    f"{_json(payload)}")
            data = self.llm.complete_json(system, user)
            for k in chunk:
                vals = data.get(k)
                result[k] = [str(v) for v in vals] if isinstance(vals, list) and vals \
                    else [f"{k} — тестовый вариант {j+1}" for j in range(3)]
        return result

    # ── независимые колонки ──────────────────────────────────────────────────
    def _synth_column(self, cp: ColumnProfile, df: pd.DataFrame, n: int) -> list:
        name = cp.name
        real = df[name].dropna() if name in df.columns else pd.Series([], dtype=object)
        # сохраняем АБСОЛЮТНОЕ число уникальных (обрезано числом строк сэмпла),
        # а не долю — иначе низкокардинальные поля схлопываются в 1 значение
        target_distinct = min(cp.n_distinct, n) if cp.n_distinct else 1

        # PK / почти уникальные → уникальные значения
        if cp.is_pk_hypothesis or cp.unique_perc >= 99:
            return self._unique_values(cp, n)

        # категории → ресэмпл реального распределения
        if cp.categories:
            return self._resample(real, cp.categories, n)

        # чувствительные / free-text → пул фейков
        if cp.is_sensitive or cp.semantic_class == "free_text":
            pool = self._fake_pool(cp, real, target_distinct)
            return self._draw(pool, n)

        # булевы
        if _is(cp.dtype, _BOOL):
            return self._resample(real, [True, False], n)

        # числовые не-чувствительные → в диапазоне [min,max]
        if _is(cp.dtype, _NUM) and cp.min is not None and cp.max is not None:
            return self._num_range(cp, n)

        # даты → в диапазоне
        if _is(cp.dtype, _DATE) and cp.min is not None and cp.max is not None:
            return self._date_range(cp, n)

        # прочее → ресэмпл реальных значений, иначе заглушка
        if not real.empty:
            return self._resample(real, real.unique().tolist(), n)
        return [f"SYN-{i:06d}" for i in range(n)]

    # ── примитивы генерации ──────────────────────────────────────────────────
    def _resample(self, real: pd.Series, fallback_values: list, n: int) -> list:
        if real is not None and not real.empty:
            return real.sample(n=n, replace=True, random_state=self.cfg.seed).astype(object).tolist()
        return [self.r.choice(fallback_values) for _ in range(n)]

    def _unique_values(self, cp: ColumnProfile, n: int) -> list:
        if _is(cp.dtype, _INT):
            start = 1
            return list(range(start, start + n))
        if _is(cp.dtype, _NUM):
            return [round(1 + i + self.r.random(), 2) for i in range(n)]
        if cp.is_sensitive and cp.sensitive_kind:
            base = self.faker.pool(cp.sensitive_kind, n)
            return [f"{v}-{i:05d}" for i, v in enumerate(base)]
        return [f"SYN-{cp.name[:6].upper()}-{i:06d}" for i in range(n)]

    def _fake_pool(self, cp: ColumnProfile, real: pd.Series, distinct: int) -> list:
        # LLM (только для текстов) дорог → пул ограничен llm_pool_size, строки
        # набираются ресэмплом. Фейкер дёшев и офлайн → генерит полную кардинальность.
        if self.llm and cp.semantic_class == "free_text":
            try:
                examples = [str(v) for v in real.astype(str).unique().tolist()[:8]]
                return self._llm_flat_pool(cp.name, examples, min(distinct, self.cfg.llm_pool_size))
            except LLMError as exc:  # noqa: BLE001
                logger.warning("LLM-пул %s: %s — фолбэк-фейкер", cp.name, exc)
        size = max(1, distinct)
        if cp.is_sensitive and cp.sensitive_kind:
            return self.faker.pool(cp.sensitive_kind, size)
        return [f"SYN-{cp.name[:6].upper()}-{i:05d}" for i in range(size)]

    def _fallback_pool(self, cp: ColumnProfile) -> list:
        if cp.is_sensitive and cp.sensitive_kind:
            return self.faker.pool(cp.sensitive_kind, 8)
        return [f"SYN-{cp.name[:6].upper()}-{i:03d}" for i in range(8)]

    def _llm_flat_pool(self, name: str, examples: list[str], size: int) -> list:
        system = (
            "Ты генерируешь СИНТЕТИЧЕСКИЕ значения поля для тестовой БД: похожие по "
            "смыслу/формату на реальные примеры, но полностью вымышленные. "
            'Ответ строго JSON: {"values": ["...", ...]}.'
        )
        user = (f"Поле: {name}\nСгенерируй {size} значений.\n"
                f"Реальные примеры (стиль/формат): {_json(examples)}")
        data = self.llm.complete_json(system, user)
        vals = data.get("values") if isinstance(data, dict) else None
        if not isinstance(vals, list) or not vals:
            raise LLMError("пустой values")
        return [str(v) for v in vals]

    def _draw(self, pool: list, n: int) -> list:
        if not pool:
            return [None] * n
        if len(pool) >= n:
            return self.r.sample(pool, n)
        vals = list(pool)                              # каждый distinct хотя бы раз
        vals += [self.r.choice(pool) for _ in range(n - len(pool))]
        self.r.shuffle(vals)
        return vals

    def _num_range(self, cp: ColumnProfile, n: int) -> list:
        lo, hi = float(cp.min), float(cp.max)
        if _is(cp.dtype, _INT):
            lo, hi = int(lo), int(hi)
            if lo > hi:
                lo, hi = hi, lo
            return [self.r.randint(lo, hi) for _ in range(n)]
        if hi < lo:
            lo, hi = hi, lo
        return [round(self.r.uniform(lo, hi), 4) for _ in range(n)]

    def _date_range(self, cp: ColumnProfile, n: int) -> list:
        lo = pd.Timestamp(cp.min)
        hi = pd.Timestamp(cp.max)
        if pd.isna(lo) or pd.isna(hi):
            return [None] * n
        span = max((hi - lo).total_seconds(), 0.0)
        out = []
        for _ in range(n):
            ts = lo + pd.Timedelta(seconds=self.r.random() * span)
            out.append(ts.isoformat())
        return out

    def _apply_nulls(self, frame: pd.DataFrame, name: str, not_null_perc: float, n: int) -> None:
        n_null = int(round((100 - not_null_perc) / 100 * n))
        if n_null <= 0:
            return
        pos = self.r.sample(range(n), min(n_null, n))
        col = frame[name].astype(object)
        col.iloc[pos] = None
        frame[name] = col


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)

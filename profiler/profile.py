"""Профилирование таблицы по сэмплу — ТОЛЬКО pandas, без нагрузки на БД.

На выходе по каждой колонке: тип, доля null, кардинальность (n_distinct и
unique_perc), min/max (для не-чувствительных числовых/дат), полный список
категорий (если уник. <= max_categories и колонка не чувствительная), длины
строк, примеры значений, semantic_class и флаг чувствительности.

PK — ГИПОТЕЗА: минимальная уникальная комбинация колонок на сэмпле (в DDL
Greenplum ключей нет). Агент обязан подтверждать её живой пробой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import combinations

import pandas as pd

from .sensitivity import classify, detect_by_values

_METRIC_RE = re.compile(
    r"(^|_)(qty|quantity|amt|amount|sum|total|cnt|count|avg|rate|ratio|pct|perc|val|value)($|_)", re.I
)
_SYS_TS_RE = re.compile(
    r"(dttm$|timestamp|inserted|modified|updated|_update_|^update_|load_)", re.I
)
_ID_RE = re.compile(r"(^|_)(id|code|key|inn|kpp|ogrn)($|_)", re.I)

_DATE_TYPES = ("date", "timestamp", "time")
_NUM_TYPES = ("int", "numeric", "decimal", "double", "real", "float", "smallint", "bigint", "money")
_BOOL_TYPES = ("bool",)
_TEXT_TYPES = ("char", "text", "uuid", "json")


@dataclass
class ColumnProfile:
    name: str
    dtype: str                    # SQL-тип из information_schema
    is_nullable: bool
    description: str = ""          # из комментария (с redirect sn_uzp→sn_view)
    semantic_class: str = ""      # flag/date/metric/join_key/enum_like/label/free_text
    is_sensitive: bool = False
    sensitive_kind: str = ""      # inn/fio/money/... — подсказка фейкеру
    not_null_perc: float = 0.0
    unique_perc: float = 0.0
    n_distinct: int = 0
    is_pk_hypothesis: bool = False
    min: object = None            # только не-чувствительные числа/даты
    max: object = None
    len_min: int | None = None    # длины строк (для текстов)
    len_max: int | None = None
    categories: list[str] | None = None   # ВСЕ значения, если это категория
    sample_values: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "name": self.name, "dtype": self.dtype, "is_nullable": self.is_nullable,
            "description": self.description,
            "semantic_class": self.semantic_class, "is_sensitive": self.is_sensitive,
            "sensitive_kind": self.sensitive_kind, "not_null_perc": self.not_null_perc,
            "unique_perc": self.unique_perc, "n_distinct": self.n_distinct,
            "is_pk_hypothesis": self.is_pk_hypothesis,
            "min": _jsonable(self.min), "max": _jsonable(self.max),
            "len_min": self.len_min, "len_max": self.len_max,
            "categories": self.categories, "sample_values": self.sample_values,
        }
        return d


@dataclass
class TableProfile:
    schema: str
    table: str
    description: str
    est_rows: int
    sample_rows: int
    sample_fraction: float
    pk_hypothesis: list[str]
    columns: list[ColumnProfile]

    @property
    def fqn(self) -> str:
        return f"{self.schema}.{self.table}"

    def to_dict(self) -> dict:
        return {
            "schema": self.schema, "table": self.table, "fqn": self.fqn,
            "description": self.description, "est_rows": self.est_rows,
            "sample_rows": self.sample_rows, "sample_fraction": round(self.sample_fraction, 6),
            "pk_hypothesis": self.pk_hypothesis,
            "columns": [c.to_dict() for c in self.columns],
        }


def _jsonable(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    try:
        import numpy as np
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            return float(v)
    except Exception:  # noqa: BLE001
        pass
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _is_metric(name: str) -> bool:
    return bool(_METRIC_RE.search(name or ""))


def _classify(name: str, dtype: str, unique_pct: float, n_distinct: int) -> str:
    d = (dtype or "").lower()
    if any(b in d for b in _BOOL_TYPES):
        return "flag"
    if any(t in d for t in _DATE_TYPES):
        return "date"
    if any(t in d for t in _NUM_TYPES):
        if _is_metric(name):
            return "metric"
        if _ID_RE.search(name):
            return "join_key"
        return "metric" if unique_pct > 50 else "join_key"
    if any(t in d for t in _TEXT_TYPES):
        if _ID_RE.search(name):
            return "join_key"
        if n_distinct <= 50 and unique_pct < 20:
            return "enum_like"
        if unique_pct > 80:
            return "free_text"
        return "label"
    return "attribute"


def find_pk(df: pd.DataFrame, max_cols: int = 4) -> list[str]:
    """Минимальная уникальная комбинация на сэмпле. Метрики и СИСТЕМНЫЕ
    таймстемпы (load/inserted/_dttm) откладываем — иначе почти-уникальный
    служебный столбец ложно становится ключом."""
    if df.empty:
        return []
    cols = [c for c in df.columns if df[c].notna().all() and df[c].nunique(dropna=False) > 1]
    if not cols:
        return []

    def low_priority(c: str) -> bool:
        return _is_metric(c) or bool(_SYS_TS_RE.search(c))

    preferred = [c for c in cols if not low_priority(c)]
    deferred = [c for c in cols if low_priority(c)]
    for candidates in ([preferred] if preferred else []) + [preferred + deferred]:
        upper = min(max_cols, len(candidates))
        for size in range(1, upper + 1):
            for combo in combinations(candidates, size):
                if not df.duplicated(subset=list(combo)).any():
                    return list(combo)
    return []


def _minmax(series: pd.Series, dtype: str):
    d = (dtype or "").lower()
    s = series.dropna()
    if s.empty:
        return None, None
    if any(t in d for t in _NUM_TYPES):
        s = pd.to_numeric(s, errors="coerce").dropna()
    elif any(t in d for t in _DATE_TYPES):
        s = pd.to_datetime(s, errors="coerce").dropna()
    else:
        return None, None
    if s.empty:
        return None, None
    return s.min(), s.max()


def profile_column(series: pd.Series, meta: dict, n: int, max_categories: int,
                   force_sensitive: dict | None = None,
                   force_non_sensitive: set | None = None) -> ColumnProfile:
    name, dtype = meta["column_name"], meta["data_type"]
    is_nullable = meta["is_nullable"] == "YES"
    is_sensitive, kind = classify(name, force_sensitive=force_sensitive,
                                  force_non_sensitive=force_non_sensitive)

    non_null = series.dropna()

    # детект PII по СОДЕРЖИМОМУ (напр. author_login с ФИО/email внутри значения),
    # если не поймано по имени и колонка не в белом списке
    if not is_sensitive and (name or "").lower() not in (force_non_sensitive or set()) \
            and non_null.dtype == object and not non_null.empty:
        d_sens, d_kind = detect_by_values(non_null.astype(str).tolist()[:500])
        if d_sens:
            is_sensitive, kind = True, d_kind
    nn_perc = round(float(series.notna().mean() * 100), 2) if n else 0.0
    n_distinct = int(non_null.nunique()) if n else 0
    uniq_perc = round(n_distinct / n * 100, 2) if n else 0.0
    sclass = _classify(name, dtype, uniq_perc, n_distinct)

    cp = ColumnProfile(
        name=name, dtype=dtype, is_nullable=is_nullable, semantic_class=sclass,
        is_sensitive=is_sensitive, sensitive_kind=kind,
        not_null_perc=nn_perc, unique_perc=uniq_perc, n_distinct=n_distinct,
    )

    # min/max — только для НЕ чувствительных числовых/дат (иначе утечка диапазона).
    if not is_sensitive:
        cp.min, cp.max = _minmax(non_null, dtype)

    # длины строк (полезно синтезатору/агенту, не утечка)
    if not non_null.empty and non_null.dtype == object:
        lens = non_null.astype(str).str.len()
        cp.len_min, cp.len_max = int(lens.min()), int(lens.max())

    # категории: перечисляем ВСЕ значения, если их немного и колонка не чувствительна
    if not is_sensitive and 0 < n_distinct <= max_categories:
        vals = non_null.astype(str).map(str.strip)
        cp.categories = sorted(v for v in vals.unique().tolist() if v != "")

    # примеры значений (для чувствительных — не сохраняем реальные)
    if not is_sensitive and not non_null.empty:
        cp.sample_values = [str(v) for v in non_null.astype(str).unique().tolist()[:10]]

    return cp


def profile_table(df: pd.DataFrame, cols_meta: list[dict], *, schema: str, table: str,
                  description: str, est_rows: int, sample_fraction: float,
                  max_categories: int, force_sensitive: dict | None = None,
                  force_non_sensitive: set | None = None) -> TableProfile:
    n = len(df)
    pk = find_pk(df)
    columns: list[ColumnProfile] = []
    for cm in cols_meta:
        name = cm["column_name"]
        series = df[name] if name in df.columns else pd.Series([], dtype=object)
        cp = profile_column(series, cm, n, max_categories,
                            force_sensitive=force_sensitive,
                            force_non_sensitive=force_non_sensitive)
        cp.is_pk_hypothesis = name in pk
        columns.append(cp)
    return TableProfile(
        schema=schema, table=table, description=description, est_rows=est_rows,
        sample_rows=n, sample_fraction=sample_fraction, pk_hypothesis=pk, columns=columns,
    )

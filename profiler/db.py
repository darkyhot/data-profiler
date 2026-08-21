"""Слой БД: подключение, дешёвый сэмплинг, интроспекция, комментарии.

Дизайн: НИКАКОЙ тяжёлой нагрузки на БД. Сэмпл берём одним проходом
`SELECT * WHERE random() < frac LIMIT n`, где frac рассчитан от оценки размера
таблицы (pg_class.reltuples) так, чтобы не сканировать всё и получить ~n строк.
Все вычисления над данными — уже в pandas (см. profile.py / synth.py).

Комментарии-описания читаем с redirect-заглушкой из исходного проекта:
для схемы *_sn_uzp описания живут в парной *_sn_view (та же таблица), иначе —
в своей схеме (напр. *_sn_t_uzp читает сам себя).
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_KRB_HINT = "Kerberos-тикет истёк (GSSAPI). Обнови тикет: kinit, затем перезапусти прогон."
_KRB_TOKENS = ("kerberos", "gssapi", "krb5", "ticket", "credentials cache", "no credentials")


class KerberosExpiredError(RuntimeError):
    """Истёк Kerberos-тикет / ошибка GSSAPI — прогон надо остановить и сделать kinit."""


def is_kerberos_error(exc: object) -> bool:
    s = str(exc).lower()
    return any(t in s for t in _KRB_TOKENS)


def _raise_if_kerberos(exc: BaseException) -> None:
    if is_kerberos_error(exc):
        raise KerberosExpiredError(_KRB_HINT) from exc


def _short(sql: str, limit: int = 500) -> str:
    """SQL в одну строку для лога (обрезка длинных)."""
    s = " ".join(str(sql).split())
    return s if len(s) <= limit else s[:limit] + " …"


class Db:
    def __init__(self, url: str, *, connect_timeout_s: int = 10, statement_timeout_s: int = 0):
        connect_args: dict = {"connect_timeout": connect_timeout_s}
        # Жёсткий предел на КАЖДЫЙ запрос: если скан огромной вьюхи «виснет», запрос
        # прервётся по таймауту (а не бесконечно), в логе будет видно на какой операции.
        if statement_timeout_s and statement_timeout_s > 0:
            connect_args["options"] = f"-c statement_timeout={int(statement_timeout_s) * 1000}"
        self.engine: Engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        self.statement_timeout_s = statement_timeout_s

    @contextmanager
    def _timed(self, label: str, sql: str | None = None):
        """Лог: что за операция + SQL до запроса, и сколько заняла — после.
        Так в консоли видно, на каком именно запросе к БД скрипт стоит."""
        if sql is not None:
            logger.info("→ %s | SQL: %s", label, _short(sql))
        else:
            logger.info("→ %s", label)
        t = time.perf_counter()
        try:
            yield
        finally:
            logger.info("← %s | %.2f c", label, time.perf_counter() - t)

    # ── оценка размера и сэмплинг ────────────────────────────────────────────
    def estimate_rows(self, schema: str, table: str) -> int:
        """Оценка числа строк из статистики каталога (мгновенно, без скана).
        На Greenplum/Postgres reltuples обновляется ANALYZE. -1/0 → неизвестно."""
        sql = text(
            "SELECT c.reltuples::bigint AS n FROM pg_class c "
            "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
            "WHERE ns.nspname = :s AND c.relname = :t"
        )
        try:
            with self._timed(f"estimate_rows {schema}.{table}"), self.engine.connect() as conn:
                row = conn.execute(sql, {"s": schema, "t": table}).fetchone()
            n = int(row[0]) if row and row[0] is not None else 0
            return max(n, 0)
        except Exception as exc:  # noqa: BLE001
            _raise_if_kerberos(exc)
            logger.warning("estimate_rows %s.%s: %s", schema, table, exc)
            return 0

    def _sample_fraction(self, est_rows: int, sample_rows: int) -> float:
        """Коэффициент для WHERE random() < frac. Берём с запасом x3 (random()
        отсекает примерно долю строк, LIMIT добивает точность). Неизвестен
        размер → тянем всё до LIMIT (frac=1)."""
        if est_rows <= 0 or est_rows <= sample_rows:
            return 1.0
        return min(1.0, (sample_rows * 3.0) / est_rows)

    def sample_df(self, schema: str, table: str, sample_rows: int) -> tuple[pd.DataFrame, int, float]:
        """Сэмпл таблицы в pandas. Возвращает (df, est_rows, frac).
        Запрос: SELECT * FROM s.t WHERE random() < frac LIMIT n."""
        est = self.estimate_rows(schema, table)
        frac = self._sample_fraction(est, sample_rows)
        ident = f'"{schema}"."{table}"'
        if frac >= 1.0:
            sql = f"SELECT * FROM {ident} LIMIT {int(sample_rows)}"
        else:
            sql = f"SELECT * FROM {ident} WHERE random() < {frac:.6f} LIMIT {int(sample_rows)}"
        logger.info("sample %s.%s: est=%s frac=%.5f limit=%s", schema, table, est, frac, sample_rows)
        with self._timed(f"сэмпл {schema}.{table}", sql), self.engine.connect() as conn:
            df = pd.read_sql(text(sql), conn)
        return df, est, frac

    def read_full(self, schema: str, table: str) -> tuple[pd.DataFrame, int]:
        """Вся таблица целиком (для справочников). Возвращает (df, n).
        Без сэмплинга — справочники малы и должны быть полными."""
        ident = f'"{schema}"."{table}"'
        sql = f"SELECT * FROM {ident}"
        with self._timed(f"read_full {schema}.{table} (ВСЯ таблица)", sql), self.engine.connect() as conn:
            df = pd.read_sql(text(sql), conn)
        logger.info("full %s.%s: строк=%d", schema, table, len(df))
        return df, len(df)

    def distinct_values(self, schema: str, table: str, cols: list[str], *,
                        timeout_s: int = 1200, batch_cols: int = 5) -> dict[str, list[str]]:
        """Точный ПОЛНЫЙ набор значений для нескольких НИЗКОКАРДИНАЛЬНЫХ колонок.
        Скошенные редкие категории не попадают в сэмпл, а pg_stats на вьюхах нет.

        Агрегировать напрямую по цепочке вьюх дорого/виснет, поэтому:
        1) материализуем во временную таблицу my_tab ТОЛЬКО нужные колонки-кандидаты
           (не SELECT * — на широких таблицах это в разы меньше данных);
        2) array_agg(DISTINCT …) считаем ПАЧКАМИ по batch_cols колонок (несколько
           distinct-агрегатов в одном запросе на Greenplum жрут память/спиллят).
        Всё в одном соединении/транзакции; на КАЖДЫЙ statement — statement_timeout
        (LOCAL, сбрасывается в конце транзакции). Kerberos-ошибка пробрасывается
        (прогон надо остановить). Прочая ошибка/таймаут → вернуть уже собранное
        (частичный результат), остальные колонки останутся по сэмплу."""
        if not cols:
            return {}
        ident = f'"{schema}"."{table}"'
        cols_sql = ", ".join(f'"{c}"' for c in cols)
        step = batch_cols if batch_cols and batch_cols > 0 else len(cols)
        batches = [cols[i:i + step] for i in range(0, len(cols), step)]
        out: dict[str, list[str]] = {}
        label = (f"добор категорий {schema}.{table} ({len(cols)} колонок, "
                 f"temp my_tab, {len(batches)} пачк(и) по {step})")
        try:
            with self._timed(label), self.engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_s) * 1000}"))
                conn.execute(text("DROP TABLE IF EXISTS my_tab"))
                create_sql = f"CREATE TEMP TABLE my_tab AS SELECT {cols_sql} FROM {ident}"
                with self._timed(f"материализация temp my_tab ← {ident} ({len(cols)} колонок)", create_sql):
                    conn.execute(text(create_sql))
                for k, chunk in enumerate(batches, 1):
                    agg = ", ".join(f'array_agg(DISTINCT "{c}"::text) AS "{c}"' for c in chunk)
                    agg_sql = f"SELECT {agg} FROM my_tab"
                    with self._timed(f"array_agg my_tab батч {k}/{len(batches)} ({len(chunk)} колонок)", agg_sql):
                        row = conn.execute(text(agg_sql)).mappings().first()
                    if row:
                        for c in chunk:
                            vals = row.get(c) or []
                            out[c] = [str(v) for v in vals if v is not None]
        except Exception as exc:  # noqa: BLE001
            _raise_if_kerberos(exc)
            logger.warning("distinct_values %s.%s: %s (собрано колонок: %d)",
                           schema, table, exc, len(out))
        return out

    def verify_unique(self, schema: str, table: str, cols: list[str]) -> bool:
        """Точная проверка уникальности комбинации на ПОЛНОЙ таблице (один
        агрегат). True — дубликатов нет (это точный PK). При ошибке → False.
        NULL в ключе тоже ловится: строки с NULL группируются и дают дубль."""
        if not cols:
            return False
        ident = f'"{schema}"."{table}"'
        cols_sql = ", ".join(f'"{c}"' for c in cols)
        sql = f"SELECT 1 FROM {ident} GROUP BY {cols_sql} HAVING count(*) > 1 LIMIT 1"
        try:
            with self._timed(f"проверка PK {schema}.{table} {cols} (СКАН вьюхи)", sql), \
                    self.engine.connect() as conn:
                dup = conn.execute(text(sql)).fetchone()
            return dup is None
        except Exception as exc:  # noqa: BLE001
            _raise_if_kerberos(exc)
            logger.warning("verify_unique %s.%s %s: %s", schema, table, cols, exc)
            return False

    # ── интроспекция схемы ───────────────────────────────────────────────────
    def introspect_columns(self, schema: str, table: str) -> list[dict]:
        """Колонки таблицы: имя, тип, nullable — в порядке ordinal_position."""
        sql = text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t "
            "ORDER BY ordinal_position"
        )
        with self._timed(f"колонки {schema}.{table}"), self.engine.connect() as conn:
            rows = conn.execute(sql, {"s": schema, "t": table}).fetchall()
        return [{"column_name": r[0], "data_type": r[1], "is_nullable": r[2]} for r in rows]

    def table_exists(self, schema: str, table: str) -> bool:
        sql = text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :s AND table_name = :t LIMIT 1"
        )
        with self.engine.connect() as conn:
            return conn.execute(sql, {"s": schema, "t": table}).fetchone() is not None

    # ── комментарии-описания (с redirect-заглушкой) ──────────────────────────
    @staticmethod
    def comments_schema(schema: str) -> str:
        """Redirect из исходного проекта: описания для *_sn_uzp лежат в парной
        *_sn_view (та же таблица). Для *_sn_t_uzp и прочих — своя схема.
        Проверяем _sn_uzp ДО _uzp, чтобы sn_t_uzp не попал под редирект."""
        s = schema
        if s.endswith("_sn_uzp"):
            # <prefix>_ld_..._sn_uzp  →  <prefix>_as_..._sn_view (как в проде)
            base = s[: -len("_sn_uzp")]
            base = base.replace("_ld_", "_as_")
            return f"{base}_sn_view"
        if s == "sn_uzp":                    # короткая форма из примера пользователя
            return "sn_view"
        return s

    def read_comments(self, schema: str, table: str) -> tuple[str, dict[str, str]]:
        """(комментарий таблицы, {колонка: комментарий}) с учётом redirect.
        Сначала пробуем redirect-схему; если там пусто — читаем свою."""
        redirect = self.comments_schema(schema)
        if redirect != schema:
            tc, cc = self._read_comments_raw(redirect, table)
            if tc or any(cc.values()):
                return tc, cc
        return self._read_comments_raw(schema, table)

    def _read_comments_raw(self, schema: str, table: str) -> tuple[str, dict[str, str]]:
        reg = f"{schema}.{table}"
        table_sql = text("SELECT obj_description(to_regclass(:reg), 'pg_class')")
        col_sql = text(
            "SELECT a.attname, col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass(:reg) AND a.attnum > 0 AND NOT a.attisdropped"
        )
        try:
            with self._timed(f"комментарии {reg}"), self.engine.connect() as conn:
                tc = conn.execute(table_sql, {"reg": reg}).scalar()
                col_rows = conn.execute(col_sql, {"reg": reg}).fetchall()
        except Exception as exc:  # noqa: BLE001
            _raise_if_kerberos(exc)
            logger.warning("read_comments %s: %s", reg, exc)
            return "", {}
        cols = {r[0]: (r[1] or "") for r in col_rows}
        return (tc or ""), cols

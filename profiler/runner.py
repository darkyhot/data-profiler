"""Оркестрация прогона: по каждой таблице манифеста —
профиль (pandas) + синтетический сэмпл (LLM/фейкер) → файлы.

Точка входа для ячейки тетрадки: profiler.run(locals()).
"""

from __future__ import annotations

import logging
import sys

from . import io
from .config import RunConfig, from_namespace
from .db import Db
from .env import load_dotenv
from .llm import LLMClient
from .profile import profile_table
from .synth import Synthesizer

logger = logging.getLogger("profiler")


def _setup_logging() -> None:
    if logger.handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def run(ns) -> dict:
    """ns — dict переменных ячейки (обычно locals()) или готовый RunConfig."""
    _setup_logging()
    load_dotenv()                       # подхватить токены из .env (если есть)
    cfg = ns if isinstance(ns, RunConfig) else from_namespace(dict(ns))
    db = Db(cfg.db_url)
    llm = LLMClient(cfg.llm)
    if llm.enabled and not (llm.base_url and llm.token):
        logger.warning("LLM method=%s, но base_url/token пусты — синтез уйдёт в фолбэк-фейкер",
                       cfg.llm.method)
    synth = Synthesizer(cfg, llm)

    entries: list[dict] = []
    for fqn in cfg.tables:
        schema, table = fqn.split(".", 1)
        logger.info("=== %s ===", fqn)
        try:
            entry = _process_table(cfg, db, synth, schema, table)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Таблица %s пропущена: %s", fqn, exc)
            entries.append({"fqn": fqn, "status": "error", "error": str(exc)})
            continue
        entries.append(entry)

    manifest = io.write_manifest(cfg.output_dir, entries)
    ok = sum(1 for e in entries if e.get("status") == "ok")
    logger.info("Готово: %d/%d таблиц. Манифест: %s", ok, len(cfg.tables), manifest)
    return {"entries": entries, "manifest": str(manifest), "output_dir": str(cfg.output_dir)}


def _process_table(cfg: RunConfig, db: Db, synth: Synthesizer, schema: str, table: str) -> dict:
    fqn = f"{schema}.{table}"
    cols_meta = db.introspect_columns(schema, table)
    if not cols_meta:
        raise ValueError(f"Таблица {fqn} не найдена или без колонок")
    table_comment, col_comments = db.read_comments(schema, table)
    is_full = fqn in cfg.full_tables
    if is_full:                                    # справочник — целиком, без сэмплинга
        df, n_rows = db.read_full(schema, table)
        est, frac = n_rows, 1.0
    else:
        df, est, frac = db.sample_df(schema, table, cfg.sample_rows_profile)

    description = table_comment or table.replace("_", " ")
    profile = profile_table(
        df, cols_meta, schema=schema, table=table, description=description,
        est_rows=est, sample_fraction=frac, max_categories=cfg.max_categories,
        force_sensitive=cfg.sensitive_columns, force_non_sensitive=cfg.non_sensitive_columns,
    )
    # описания колонок из комментариев (redirect уже учтён в read_comments)
    for cp in profile.columns:
        if col_comments.get(cp.name):
            cp.description = col_comments[cp.name]

    if is_full:
        # справочник целиком: реальные данные, маскируем только персональные поля
        sample_df = synth.mask_full_table(profile, df, force_sensitive=cfg.sensitive_columns)
    else:
        masked = [c.name for c in profile.columns if c.is_sensitive]
        if masked:
            logger.info("%s: маскируются (проверь на ложные срабатывания): %s", fqn, masked)
        groups = [g for g in cfg.correlated_groups
                  if sum(1 for c in g if c in {cm["column_name"] for cm in cols_meta}) >= 2]
        sample_df = synth.synth_table(profile, df, groups)

    p_path = io.write_profile(cfg.output_dir, profile)
    s_path = io.write_sample(cfg.output_dir, fqn, sample_df)
    logger.info("%s: профиль=%s сэмпл=%s (%d строк, %s, pk=%s)", fqn, p_path.name, s_path.name,
                len(sample_df), "СПРАВОЧНИК целиком" if is_full else "синтетика", profile.pk_hypothesis)
    return {
        "fqn": fqn, "status": "ok", "mode": "full" if is_full else "synth",
        "est_rows": est, "sample_rows_profiled": len(df),
        "synth_rows": len(sample_df), "pk_hypothesis": profile.pk_hypothesis,
        "profile": str(p_path), "sample": str(s_path),
    }

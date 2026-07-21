"""Запись результата: portable-файлы, которые переносятся на открытый контур.

По каждой таблице:
  <output>/profiles/<schema>.<table>.profile.json  — профиль значений
  <output>/samples/<schema>.<table>.sample.csv      — синтетический сэмпл
Плюс общий manifest.json со сводкой прогона.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .profile import TableProfile


def write_profile(out_dir: Path, profile: TableProfile) -> Path:
    d = out_dir / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{profile.fqn}.profile.json"
    path.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_sample(out_dir: Path, fqn: str, df: pd.DataFrame) -> Path:
    d = out_dir / "samples"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{fqn}.sample.csv"
    df.to_csv(path, index=False)
    return path


def write_manifest(out_dir: Path, entries: list[dict]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "manifest.json"
    path.write_text(json.dumps({"tables": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

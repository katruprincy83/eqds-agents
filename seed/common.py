"""Shared helpers for the synthetic data generators. SYNTHETIC DEMO DATA.

Pure python/pandas/numpy so the generators run (and are unit-tested) without Spark.
Jobs only add the Spark read/write layer around them.
"""
from __future__ import annotations

import datetime as dt
import pathlib

import numpy as np
import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parent


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def load_planted() -> dict:
    return yaml.safe_load((ROOT / "planted_facts.yaml").read_text())


def d(s: str | dt.date) -> dt.date:
    return s if isinstance(s, dt.date) else dt.date.fromisoformat(s)


def rng(cfg: dict, salt: int = 0) -> np.random.Generator:
    return np.random.default_rng(int(cfg["seed"]) * 1000 + salt)


def business_days(start: dt.date, end: dt.date) -> list[dt.date]:
    return [x.date() for x in pd.bdate_range(start, end)]


def month_ends(cfg: dict) -> list[dt.date]:
    return [d(x) for x in cfg["month_ends"]]


SYNTHETIC_COMMENT = "SYNTHETIC DEMO DATA"

FX = {"JPY": 150.0, "CNY": 7.2, "HKD": 7.8, "INR": 84.0, "TWD": 32.0, "KRW": 1350.0, "BRL": 5.4, "SAR": 3.75,
      "USD": 1.0, "ILS": 3.7, "OTH": 1.0}
CCY = {"Japan": "JPY", "China": "CNY", "India": "INR", "Taiwan": "TWD", "Korea": "KRW", "Brazil": "BRL",
       "Saudi Arabia": "SAR", "United States": "USD", "Israel": "ILS"}

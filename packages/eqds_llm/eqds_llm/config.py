"""Tier configuration from `config/models.<target>.yaml` (design §8.1). Every tier is a Unity AI Gateway model service."""
from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass, field
from string import Template

import yaml

TIERS = ("reasoning", "standard", "fast")


def _ident(s: str) -> str:
    """UC object names: letters, digits, underscore."""
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


@dataclass
class Tier:
    primary: str                      # model service the tier calls: catalog.schema.service
    fallback: str | None = None       # second model service (via the same gateway) used after retries are exhausted
    max_output_tokens: int = 2048
    model: str = ""                   # UC model behind the primary destination (pricing)
    gateway_fallback: tuple[str, ...] = ()


@dataclass
class Retry:
    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 16.0
    retry_on: tuple[int, ...] = (429, 500, 502, 503, 504)


@dataclass
class ModelConfig:
    tiers: dict[str, Tier]
    retry: Retry = field(default_factory=Retry)
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    embedding_endpoint: str = ""
    gateway_url: str = ""
    price_keys: dict[str, str] = field(default_factory=dict)   # service name -> UC model name, for cost estimates

    def tier(self, name: str) -> Tier:
        if name not in self.tiers:
            raise KeyError(f"unknown model tier {name!r}; configured: {sorted(self.tiers)}")
        return self.tiers[name]


def service_name(name_prefix: str, suffix: str, tier: str) -> str:
    """Model service id (the last part of `catalog.schema.service`) for a tier."""
    return _ident(f"{name_prefix}_llm_{tier}{suffix}")


def load_model_config(path: str | os.PathLike, name_prefix: str = "eqds", suffix: str = "",
                      catalog: str | None = None, schema: str | None = None) -> ModelConfig:
    if not catalog or not schema:
        raise ValueError("load_model_config needs catalog and schema: model services are Unity Catalog objects")
    raw = yaml.safe_load(Template(pathlib.Path(path).read_text()).safe_substitute(name_prefix=name_prefix, suffix=suffix))
    tiers: dict[str, Tier] = {}
    price_keys: dict[str, str] = {}
    for k, v in raw["tiers"].items():
        primary = f"{catalog}.{schema}.{service_name(name_prefix, suffix, k)}"
        fb = v.get("client_fallback") or None
        tiers[k] = Tier(primary, fb, int(v.get("max_output_tokens", 2048)), v["model"], tuple(v.get("gateway_fallback") or ()))
        price_keys[primary] = v["model"]
        if fb:
            price_keys[fb] = (v.get("gateway_fallback") or [v["model"]])[0]
    r = raw.get("retry", {})
    retry = Retry(int(r.get("max_attempts", 4)), float(r.get("base_delay_s", 1.0)), float(r.get("max_delay_s", 16)),
                  tuple(r.get("retry_on", (429, 500, 502, 503, 504))))
    return ModelConfig(tiers, retry, raw.get("pricing_usd_per_1m", {}), (raw.get("embedding") or {}).get("endpoint", ""),
                       (raw.get("gateway") or {}).get("url", "") or "", price_keys)

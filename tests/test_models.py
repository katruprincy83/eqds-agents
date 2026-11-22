import pytest

from eqds_llm import load_model_config
from eqds_llm.client import gateway_url
from eqds_llm.config import service_name


def test_tiers_are_model_services(monkeypatch):
    cfg = load_model_config("config/models.demo.yaml", "eqds", "-x", "cat", "sch")
    assert cfg.tier("fast").primary == "cat.sch.eqds_llm_fast_x"
    assert cfg.tier("standard").fallback.startswith("system.ai.")
    assert "embed" not in cfg.tiers
    assert cfg.price_keys[cfg.tier("fast").primary] == cfg.tier("fast").model


def test_catalog_required():
    with pytest.raises(ValueError):
        load_model_config("config/models.demo.yaml", "eqds", "")


def test_gateway_url(monkeypatch):
    monkeypatch.delenv("EQDS_AI_GATEWAY_URL", raising=False)
    assert gateway_url("https://dbc-1.cloud.databricks.com", "42") == "https://42.ai-gateway.cloud.databricks.com/mlflow/v1"
    assert gateway_url("https://adb-1.2.azuredatabricks.net/", "42") == "https://42.ai-gateway.azuredatabricks.net/mlflow/v1"
    assert gateway_url("https://x.cloud.databricks.com", "42", "https://gw.example/mlflow/v1/") == "https://gw.example/mlflow/v1"


def test_service_name_is_identifier():
    assert service_name("eqds", "-stg", "fast") == "eqds_llm_fast_stg"

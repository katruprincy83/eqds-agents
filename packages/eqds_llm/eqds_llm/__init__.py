"""eqds_llm: central model access (design §8): tiers, normalization, retry + fallback, cost."""
from .client import LLM, ModelUnavailable, RateLimited
from .config import ModelConfig, Tier, load_model_config
from .cost import cost_usd
from .normalize import ChatResult, ToolCall, Usage, normalize, parse_structured

__all__ = ["LLM", "ModelConfig", "Tier", "load_model_config", "ChatResult", "ToolCall", "Usage", "normalize",
           "parse_structured", "cost_usd", "ModelUnavailable", "RateLimited"]

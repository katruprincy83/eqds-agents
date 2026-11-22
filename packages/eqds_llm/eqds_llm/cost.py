from __future__ import annotations


def cost_usd(pricing: dict[str, dict[str, float]], endpoint: str, tokens_in: int, tokens_out: int) -> float:
    p = pricing.get(endpoint) or pricing.get("default") or {"in": 0.0, "out": 0.0}
    return round((tokens_in * p["in"] + tokens_out * p["out"]) / 1_000_000, 6)

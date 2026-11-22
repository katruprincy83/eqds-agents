"""Specialist spec checks shared by the registration CLI, the registration job and the UI (all of them see untrusted specs
from standalone agents). Dependency-free so every app can vendor it."""
from __future__ import annotations

import re

REQUIRED = ("key", "display_name", "tag_prefix", "description", "domains", "owner", "version", "contract_version", "timeout_s")


class RegistrationError(Exception):
    pass


NAME = re.compile(r"[A-Za-z0-9_]{1,64}")  # every access name ends up inside a GRANT statement: letters, digits, underscore only
TOOL_TYPES = {"uc_functions", "vector_search", "bridge_mcp"}


def validate_spec(sp: dict, key: str) -> dict:
    """Checks that apply to every spec, whether it comes from the repo or from a standalone agent's /info (untrusted input)."""
    missing = [k for k in REQUIRED if sp.get(k) in (None, "", [])]
    if missing:
        raise RegistrationError(f"the spec is missing: {', '.join(missing)}")
    if sp["key"] != key:
        raise RegistrationError(f"key in the spec ('{sp['key']}') does not match '{key}'")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", key):
        raise RegistrationError("key must be lowercase letters, digits and underscores")
    if not re.fullmatch(r"[A-Z]{1,3}", str(sp["tag_prefix"])):
        raise RegistrationError("tag_prefix must be 1 to 3 capital letters (it prefixes citation tags)")
    if str(sp["contract_version"]) != "1":
        raise RegistrationError(f"unsupported contract_version {sp['contract_version']} (this platform speaks '1')")
    for t in sp.get("tools") or []:
        if t.get("type") not in TOOL_TYPES:
            raise RegistrationError(f"unsupported tool type '{t.get('type')}' (allowed: {', '.join(sorted(TOOL_TYPES))})")
    names = [n for k in ("tables", "functions", "volumes") for n in (sp.get("access") or {}).get(k) or []]
    names += [i["name"] for t in sp.get("tools") or [] if t.get("type") == "vector_search" for i in t.get("indexes") or []]
    bad = [n for n in names if not NAME.fullmatch(str(n))]
    if bad:
        raise RegistrationError(f"invalid object names in the spec: {bad}")
    return sp


def spec_from_info(info: dict, key: str) -> dict:
    """The spec of a standalone agent, rebuilt from its own /info (the runtime serves what it was started with)."""
    sp = {k: info.get(k) for k in ("key", "display_name", "tag_prefix", "description", "domains", "owner", "version", "timeout_s", "contract_version",
                                    "model_tier", "skill", "access")}
    sp["tools"] = info.get("tool_config") or []
    sp["tools_summary"] = info.get("tools_summary") or info.get("tools") or ""
    return validate_spec(sp, key)

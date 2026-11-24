"""Environment profile loader (design §4.1). The ONLY place where environment values enter the code base.

* Jobs / scripts: `Env.from_target("demo")` reads `config/env/<target>.yaml` from the repository (or the
  bundle files path in the workspace).
* Apps: the DAB passes the same values as environment variables, `Env.from_environ()` rebuilds the view.

No host, catalog, endpoint, secret name or URL is hard coded anywhere else (CI lint rule E1).
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml


def repo_root(start: str | os.PathLike | None = None) -> pathlib.Path:
    """Locate the repository / bundle root (directory that contains `config/env`)."""
    if os.environ.get("EQDS_ROOT"):
        return pathlib.Path(os.environ["EQDS_ROOT"])
    p = pathlib.Path(start or __file__).resolve()
    for cand in [p, *p.parents]:
        if (cand / "config" / "env").is_dir():
            return cand
    raise FileNotFoundError("cannot locate repo root (config/env); set EQDS_ROOT")


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@dataclass
class Env:
    raw: dict[str, Any] = field(default_factory=dict)

    # ── construction ───────────────────────────────────────────────────────────────────────────
    @classmethod
    def from_target(cls, target: str | None = None, root: str | os.PathLike | None = None) -> "Env":
        target = target or os.environ.get("EQDS_TARGET") or "demo"
        base = repo_root(root)
        raw = yaml.safe_load((base / "config" / "env" / f"{target}.yaml").read_text())
        raw.setdefault("target", target)
        raw["_root"] = str(base)
        return cls(raw)

    @classmethod
    def from_environ(cls) -> "Env":
        """Apps: the profile file is vendored into the app (build_apps.py) and selected by EQDS_TARGET; EQDS_PROFILE_JSON overrides (local dev)."""
        import json

        if os.environ.get("EQDS_PROFILE_JSON"):
            return cls(json.loads(os.environ["EQDS_PROFILE_JSON"]))
        return cls.from_target(os.environ.get("EQDS_TARGET"), os.environ.get("EQDS_ROOT") or ".")

    # ── access ─────────────────────────────────────────────────────────────────────────────────
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def __getitem__(self, k: str) -> Any:
        return self.raw[k]

    # ── derived names (E3: everything parameterised by name_prefix / suffix) ──────────────────
    @property
    def target(self) -> str: return self.raw["target"]
    @property
    def prefix(self) -> str: return self.raw.get("name_prefix", "eqds")
    @property
    def suffix(self) -> str: return self.raw.get("suffix", "")
    @property
    def host(self) -> str: return self.raw["workspace_host"].rstrip("/")
    @property
    def catalog(self) -> str: return self.raw["catalog"]
    @property
    def schema(self) -> str: return self.raw["schema"]
    @property
    def uc(self) -> str: return f"{self.catalog}.{self.schema}"
    @property
    def foreign_catalog(self) -> str: return self.raw.get("foreign_catalog", "azure_oms")
    @property
    def connection(self) -> str: return self.raw.get("connection", "azure_oms_conn")
    @property
    def secret_scope(self) -> str: return self.raw.get("secret_scope", f"{self.prefix}-bridge")
    @property
    def data_profile(self) -> str: return self.raw.get("data_profile", "synthetic")
    @property
    def external_db_mode(self) -> str: return self.raw.get("external_db_mode", "direct")
    @property
    def lakebase_project(self) -> str: return self.get("lakebase.project", f"{self.prefix}-lakebase")
    @property
    def lakebase_database(self) -> str: return self.get("lakebase.database", "databricks_postgres")
    @property
    def vs_endpoint(self) -> str: return self.raw.get("vector_search_endpoint", f"{self.prefix}-vs")
    @property
    def experiment_path(self) -> str: return self.raw.get("experiment_path", f"/Shared/{self.prefix}/{self.target}")
    @property
    def job_caller_secret_keys(self) -> tuple[str, str]:
        """(client id, client secret) of the environment's job-caller service principal. Suffixed, because environments can share the
        secret scope (dev and production do) and each must find its own principal."""
        return (f"job-caller-client-id{self.suffix}", f"job-caller-client-secret{self.suffix}")

    @property
    def traces_schema(self) -> str:
        """UC schema that stores the traces when `flags.use_uc_trace_storage` is on (it lives in `catalog`, apart from the data schema)."""
        return self.raw.get("traces_schema", "traces")

    @property
    def use_uc_trace_storage(self) -> bool:
        return bool(self.get("flags.use_uc_trace_storage", False))

    @property
    def shared_external(self) -> bool:
        """True when the external database (and its UC connection / foreign catalog) is only borrowed from another environment."""
        return bool(self.raw.get("shared_external", False))

    @property
    def moderator_experiment_path(self) -> str:
        """Traces of the moderator. A sibling of `experiment_path` (an experiment cannot contain another), so the parent folder that the platform deploy creates is enough."""
        return self.raw.get("moderator_experiment_path", f"{self.experiment_path}-moderator")

    def agent_experiment_path(self, key: str) -> str:
        """Traces of one specialist agent, created by that agent's own bundle. Same parent folder as the platform experiment."""
        return f"{self.experiment_path}-{key}"

    def app_name(self, component: str) -> str:
        return f"{self.prefix}-{component}{self.suffix}"

    def app_url(self, w, component: str, override_env: str | None = None) -> str:
        """URL of a sibling app: explicit env override (local dev) else resolved by name via the SDK (cached).
        Nothing environment-specific is baked into the app config."""
        import os

        if override_env and os.environ.get(override_env):
            return os.environ[override_env].rstrip("/")
        cache = self.__dict__.setdefault("_app_urls", {})
        if component not in cache:
            u = w.apps.get(self.app_name(component)).url
            if not u:
                raise RuntimeError(f"app {self.app_name(component)} has no URL (not deployed?)")
            cache[component] = u.rstrip("/")
        return cache[component]

    def job_name(self, job: str) -> str:
        return f"{self.prefix}-{job}-{self.target}"

    def volume_path(self, volume: str) -> str:
        return f"/Volumes/{self.catalog}/{self.schema}/{volume}"

    def table(self, name: str) -> str:
        return f"{self.catalog}.{self.schema}.{name}"

    @property
    def azure_jdbc_url(self) -> str:
        a = self.raw["azure_sql"]
        return f"jdbc:sqlserver://{a['host']}:{a.get('port', 1433)};databaseName={a['database']};encrypt=true;trustServerCertificate=false;loginTimeout=60"

    def specialists(self) -> list[str]:
        return list(self.raw.get("specialists_deploy", []))

    def to_environ(self) -> dict[str, str]:
        """Env vars handed to apps (flat view of the profile; no secret values)."""
        import json

        pub = {k: v for k, v in self.raw.items() if not k.startswith("_")}
        return {
            "EQDS_TARGET": self.target, "EQDS_PROFILE_JSON": json.dumps(pub, separators=(",", ":")),
            "UC_CATALOG": self.catalog, "UC_SCHEMA": self.schema, "APP_SUFFIX": self.suffix,
            "NAME_PREFIX": self.prefix, "EXTERNAL_DB_MODE": self.external_db_mode,
            "DATA_PROFILE": self.data_profile, "FOREIGN_CATALOG": self.foreign_catalog,
            "UC_CONNECTION": self.connection,
        }

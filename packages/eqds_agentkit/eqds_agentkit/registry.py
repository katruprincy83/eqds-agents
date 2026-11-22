"""Specialist registry client (design §5.4): Lakebase `registry.specialists` with a 60 s TTL cache."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class SpecialistInfo:
    key: str
    display_name: str
    tag_prefix: str
    description: str
    domains: list[str]
    url: str
    contract_version: str
    timeout_s: int
    enabled: bool
    version: str
    owner: str
    tools: str | None = None


class Registry:
    def __init__(self, pool, ttl_s: float = 60.0, env_name: str | None = None):
        self.pool, self.ttl, self.env_name = pool, ttl_s, env_name
        self._rows: dict[str, SpecialistInfo] = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def _load(self) -> None:
        q = "SELECT * FROM registry.specialists" + (" WHERE env = %s" if self.env_name else "") + " ORDER BY key"
        with self.pool.connection() as c:
            rows = c.execute(q, (self.env_name,) if self.env_name else None).fetchall()
        self._rows = {r["key"]: SpecialistInfo(r["key"], r["display_name"], r["tag_prefix"], r["description"], list(r["domains"] or []),
                                               r["url"], r["contract_version"], r["timeout_s"], r["enabled"], r["version"], r["owner"], r.get("tools"))
                      for r in rows}
        self._at = time.time()

    def all(self, include_disabled: bool = False, force: bool = False) -> list[SpecialistInfo]:
        with self._lock:
            if force or time.time() - self._at > self.ttl:
                self._load()
            return [s for s in self._rows.values() if include_disabled or s.enabled]

    def get(self, key: str, include_disabled: bool = False) -> SpecialistInfo | None:
        """A disabled specialist does not resolve: it cannot be called even if a stale brief or probe names it."""
        self.all()
        s = self._rows.get(key)
        return s if s and (include_disabled or s.enabled) else None

    def refresh(self) -> None:
        """Reload now (used after an enable/disable/register so the change applies immediately, not after the TTL)."""
        self.all(force=True)


def upsert_specialist(conn, sp: dict, url: str, env_name: str, app_name: str) -> None:
    conn.execute(
        """INSERT INTO registry.specialists (key, display_name, tag_prefix, description, domains, url, contract_version, timeout_s,
                                             enabled, env, version, owner, tools, app, updated_at)
           VALUES (%(key)s, %(display_name)s, %(tag_prefix)s, %(description)s, %(domains)s, %(url)s, %(cv)s, %(timeout_s)s,
                   true, %(env)s, %(version)s, %(owner)s, %(tools)s, %(app)s, now())
           ON CONFLICT (key) DO UPDATE SET display_name=EXCLUDED.display_name, tag_prefix=EXCLUDED.tag_prefix,
             description=EXCLUDED.description, domains=EXCLUDED.domains, url=EXCLUDED.url, contract_version=EXCLUDED.contract_version,
             timeout_s=EXCLUDED.timeout_s, env=EXCLUDED.env, version=EXCLUDED.version, owner=EXCLUDED.owner,
             tools=EXCLUDED.tools, app=EXCLUDED.app, updated_at=now()""",
        {"key": sp["key"], "display_name": sp["display_name"], "tag_prefix": sp["tag_prefix"], "description": " ".join(sp["description"].split()),
         "domains": sp.get("domains", []), "url": url, "cv": str(sp.get("contract_version", "1")), "timeout_s": int(sp.get("timeout_s", 90)),
         "env": env_name, "version": str(sp.get("version", (sp.get("skill") or {}).get("version", "1.0.0"))), "owner": sp.get("owner", "EQDS"),
         "tools": sp.get("tools_summary", ""), "app": app_name})

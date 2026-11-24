"""AgentMemory: a specialist's own schema agent_<key> (self-contained agents). Offline: a fake pool records the statements."""
import pathlib

import pytest

from eqds_agentkit.memory import BASELINE_MIGRATIONS, AgentMemory


class _Cur:
    def __init__(self, rows=None, one=None):
        self._rows, self._one = rows or [], one

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class _Conn:
    def __init__(self, applied: set[str]):
        self.applied, self.stmts = applied, []

    def execute(self, q, params=None):
        text = q if isinstance(q, str) else q.as_string(None) if hasattr(q, "as_string") else str(q)
        self.stmts.append((text, params))
        if text.startswith("SELECT version FROM schema_migrations"):
            return _Cur([{"version": v} for v in self.applied])
        return _Cur()


class _Pool:
    def __init__(self, applied=()):
        self.conn = _Conn(set(applied))

    def connection(self):
        conn = self.conn

        class _Ctx:
            def __enter__(self_inner):
                return conn

            def __exit__(self_inner, *a):
                return False

        return _Ctx()


def test_key_must_be_a_safe_identifier():
    for bad in ["", "News", "1news", "news-x", "news; drop table x"]:
        with pytest.raises(ValueError):
            AgentMemory(_Pool(), bad)
    assert AgentMemory(_Pool(), "news").schema == "agent_news"


def test_baseline_migration_ships_with_the_runtime():
    files = sorted(BASELINE_MIGRATIONS.glob("*.sql"))
    assert [f.name for f in files] == ["001_memory.sql"]
    assert "CREATE TABLE IF NOT EXISTS memory" in files[0].read_text()


def test_migrate_applies_baseline_then_agent_files_once(tmp_path: pathlib.Path):
    (tmp_path / "002_extra.sql").write_text("CREATE TABLE IF NOT EXISTS extra (id int);")
    pool = _Pool()
    first = AgentMemory(pool, "news").migrate([tmp_path])
    assert first == ["specialist_migrations/001_memory.sql", f"{tmp_path.name}/002_extra.sql"]
    texts = [t for t, _ in pool.conn.stmts]
    assert texts[0] == 'CREATE SCHEMA IF NOT EXISTS "agent_news"'
    assert any("SET search_path" in t for t in texts) and texts[-1] == "RESET search_path"

    # a second start applies nothing
    again = _Pool(applied=set(first))
    assert AgentMemory(again, "news").migrate([tmp_path]) == []
    assert not any(t.startswith("CREATE TABLE IF NOT EXISTS memory") for t, _ in again.conn.stmts)


def test_readers_get_usage_and_select_on_every_start():
    pool = _Pool(applied={"specialist_migrations/001_memory.sql"})
    AgentMemory(pool, "news").migrate([], readers=["ui-sp"])
    texts = [t for t, _ in pool.conn.stmts]
    assert 'GRANT USAGE ON SCHEMA "agent_news" TO "ui-sp"' in texts
    assert 'GRANT SELECT ON TABLE "agent_news"."memory" TO "ui-sp"' in texts

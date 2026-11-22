"""Lakebase helpers for run state, run events, conversation memory and specialist memory (design §7.3, §9)."""
from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb


class Store:
    def __init__(self, pool):
        self.pool = pool

    # ── runs / events ──────────────────────────────────────────────────────────────────────
    def create_run(self, run_id: str, conversation_id: str, user_id: str, caller: str, question: str, rounds: int,
                   specialists: list[str] | None, company_id: str | None, budget: dict) -> None:
        with self.pool.connection() as c:
            c.execute("""INSERT INTO ops.runs (run_id, conversation_id, user_id, caller, question, rounds, specialists, company_id, status, budget, heartbeat_at)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'QUEUED',%s, now())""",
                      (run_id, conversation_id, user_id, caller, question, rounds, specialists, company_id, Jsonb(budget)))

    def update_run(self, run_id: str, **f: Any) -> None:
        if not f:
            return
        sets = ", ".join(f"{k} = %s" for k in f)
        vals = [Jsonb(v) if isinstance(v, (dict, list)) and k in ("result", "budget") else v for k, v in f.items()]
        with self.pool.connection() as c:
            c.execute(f"UPDATE ops.runs SET {sets} WHERE run_id = %s", (*vals, run_id))

    def heartbeat(self, run_id: str) -> None:
        with self.pool.connection() as c:
            c.execute("UPDATE ops.runs SET heartbeat_at = now() WHERE run_id = %s AND status = 'RUNNING'", (run_id,))

    def get_run(self, run_id: str) -> dict | None:
        with self.pool.connection() as c:
            return c.execute("SELECT * FROM ops.runs WHERE run_id = %s", (run_id,)).fetchone()

    def stale_runs(self, older_than_s: int = 60) -> list[dict]:
        with self.pool.connection() as c:
            return c.execute("""SELECT run_id FROM ops.runs WHERE status = 'RUNNING'
                                AND heartbeat_at < now() - make_interval(secs => %s)""", (older_than_s,)).fetchall()

    def claim_stale(self, run_id: str, older_than_s: int = 60) -> bool:
        """Atomically take over a stale run (only one replica wins)."""
        with self.pool.connection() as c:
            r = c.execute("""UPDATE ops.runs SET heartbeat_at = now() WHERE run_id = %s AND status = 'RUNNING'
                             AND heartbeat_at < now() - make_interval(secs => %s) RETURNING run_id""", (run_id, older_than_s)).fetchone()
        return bool(r)

    def history(self, user_id: str, company_id: str | None = None, limit: int = 10) -> list[dict]:
        q = "SELECT run_id, question, company_id, status, created_at, cost_usd FROM ops.runs WHERE user_id = %s"
        a: list[Any] = [user_id]
        if company_id:
            q += " AND company_id = %s"
            a.append(company_id)
        q += " ORDER BY created_at DESC LIMIT %s"
        a.append(limit)
        with self.pool.connection() as c:
            return c.execute(q, a).fetchall()

    def add_event(self, run_id: str, type_: str, payload: dict) -> int:
        with self.pool.connection() as c:
            return c.execute("INSERT INTO ops.run_events (run_id, type, payload) VALUES (%s,%s,%s) RETURNING seq",
                             (run_id, type_, Jsonb(payload))).fetchone()["seq"]

    def events_after(self, run_id: str, seq: int = 0) -> list[dict]:
        with self.pool.connection() as c:
            return c.execute("SELECT seq, type, payload FROM ops.run_events WHERE run_id = %s AND seq > %s ORDER BY seq", (run_id, seq)).fetchall()

    # ── moderator conversation memory ──────────────────────────────────────────────────────
    def recent_turns(self, conversation_id: str, n: int = 5) -> list[dict]:
        with self.pool.connection() as c:
            rows = c.execute("SELECT turn, question, answer_summary FROM mem.conversation WHERE conversation_id = %s ORDER BY turn DESC LIMIT %s",
                             (conversation_id, n)).fetchall()
        return list(reversed(rows))

    def add_turn(self, conversation_id: str, question: str, answer_summary: str, run_id: str) -> None:
        with self.pool.connection() as c:
            c.execute("""INSERT INTO mem.conversation (conversation_id, turn, question, answer_summary, run_id)
                         SELECT %s, COALESCE(MAX(turn), 0) + 1, %s, %s, %s FROM mem.conversation WHERE conversation_id = %s
                         ON CONFLICT DO NOTHING""", (conversation_id, question, answer_summary, run_id, conversation_id))

    # ── specialist memory / idempotency cache ───────────────────────────────────────────────
    def cached_response(self, idempotency_key: str, ttl_s: int = 3600) -> dict | None:
        with self.pool.connection() as c:
            r = c.execute("""SELECT response FROM mem.specialist WHERE idempotency_key = %s AND created_at > now() - make_interval(secs => %s)""",
                          (idempotency_key, ttl_s)).fetchone()
        return r["response"] if r else None

    def recent_interactions(self, specialist: str, conversation_id: str, n: int = 5) -> list[dict]:
        with self.pool.connection() as c:
            rows = c.execute("""SELECT brief, response FROM mem.specialist WHERE specialist = %s AND conversation_id = %s
                                ORDER BY created_at DESC LIMIT %s""", (specialist, conversation_id, n)).fetchall()
        return list(reversed(rows))

    def put_interaction(self, specialist: str, conversation_id: str, caller: str, idempotency_key: str, brief: str, response: dict) -> None:
        with self.pool.connection() as c:
            c.execute("""INSERT INTO mem.specialist (specialist, conversation_id, caller, idempotency_key, brief, response)
                         VALUES (%s,%s,%s,%s,%s,%s)
                         ON CONFLICT (idempotency_key) DO UPDATE SET response = EXCLUDED.response, created_at = now()""",
                      (specialist, conversation_id, caller, idempotency_key, brief, Jsonb(response)))


def dumps(o: Any) -> str:
    return json.dumps(o, default=str)

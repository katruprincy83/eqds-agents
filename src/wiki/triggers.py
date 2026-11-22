"""Wiki refresh trigger rules (requirements FR-WK-01, design §10). Pure functions: unit-tested without Spark.

Per company, since its last build:
  earnings   results reported and event_date + 2 days <= today (call and results notes are in)                 -> Company, Catalysts, Debate, External View
  broker     >= 6 new broker reports, or >= 3 rating/target changes, or >= 3 reports landing within 2 days    -> External View, Debate, Catalysts
             (broker triggers wait 14 days after the last build and pause in the week before a scheduled release)
  stale30    30 days since the last build with anything new                                                    -> Catalysts
  full       a full rebuild 120 days after the last full build                                                 -> all pages
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

ALL_PAGES = ["Company", "Industry", "Catalysts", "Debate", "Internal View", "External View", "ESG"]
STALE = {"earnings": ["Company", "Catalysts", "Debate", "External View"], "broker": ["External View", "Debate", "Catalysts"], "stale30": ["Catalysts"]}
BROKER_WAIT_DAYS, FULL_REBUILD_DAYS, STALE_DAYS = 14, 120, 30


@dataclass
class Evidence:
    company_id: str
    last_build: dt.date
    last_full_build: dt.date
    reported_events: list[dt.date] = field(default_factory=list)       # results `event_date`s with status reported
    scheduled_events: list[dt.date] = field(default_factory=list)      # results dates not yet reported
    broker_dates: list[dt.date] = field(default_factory=list)          # published_at of broker reports after last_build
    rating_target_changes: int = 0
    other_new_items: int = 0                                           # expert / earnings docs / news since last build


@dataclass
class Decision:
    company_id: str
    mode: str                                   # full | refresh | none
    triggers: list[str]
    stale_pages: list[str]


def _three_within_two_days(dates: list[dt.date]) -> bool:
    ds = sorted(dates)
    return any((ds[i + 2] - ds[i]).days <= 2 for i in range(len(ds) - 2))


def evaluate(e: Evidence, today: dt.date) -> Decision:
    if (today - e.last_full_build).days >= FULL_REBUILD_DAYS:
        return Decision(e.company_id, "full", ["full_rebuild_120d"], list(ALL_PAGES))
    triggers: list[str] = []
    pages: set[str] = set()
    if any(e.last_build < d and d + dt.timedelta(days=2) <= today for d in e.reported_events):
        triggers.append("earnings")
        pages.update(STALE["earnings"])
    since = (today - e.last_build).days
    paused = any(0 <= (d - today).days <= 7 for d in e.scheduled_events)
    if since >= BROKER_WAIT_DAYS and not paused:
        if len(e.broker_dates) >= 6:
            triggers.append("broker_6_reports")
        if e.rating_target_changes >= 3:
            triggers.append("broker_3_changes")
        if _three_within_two_days(e.broker_dates):
            triggers.append("broker_3_in_2_days")
        if any(t.startswith("broker") for t in triggers):
            pages.update(STALE["broker"])
    if since >= STALE_DAYS and (e.broker_dates or e.other_new_items or e.rating_target_changes) and "stale30" not in triggers:
        triggers.append("stale30")
        pages.update(STALE["stale30"])
    if not triggers:
        return Decision(e.company_id, "none", [], [])
    return Decision(e.company_id, "refresh", triggers, [p for p in ALL_PAGES if p in pages])

import glob
import json
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_citation_validator():
    from agents.moderator.graph import check_citations

    ok = check_citations(["GIC net sold USD 83.0m of Kioxia [US1]."], {"US1"})
    assert ok["ok"] and ok["coverage"] == 1.0
    bad = check_citations(["GIC net sold USD 83.0m of Kioxia [US9]. Another 12 names too."], {"US1"})
    assert not bad["ok"] and bad["bad_tags"] == ["US9"] and len(bad["uncited"]) == 2


def test_external_render_allowlist_and_parameterisation():
    from eqds_agentkit.external_query import render

    sql, params = render("oms_trade_flows", {"company_ids": ["C0101"], "start": "2026-10-22", "end": "2026-11-20", "group_by": "strategy"}, "c.s", "fc")
    assert "C0101" not in sql.replace("'C0101'", "") or "'C0101'" in sql
    assert params == {"start": "2026-10-22", "end": "2026-11-20"}
    with pytest.raises(Exception):
        render("drop_everything", {}, "c.s", "fc")
    with pytest.raises(Exception):
        render("oms_trade_flows", {"company_ids": ["C0101'; DROP TABLE x;--"], "start": "2026-10-22", "end": "2026-11-20", "group_by": "company"}, "c.s", "fc")


def test_manifest_and_portability_gate():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "manifest_check.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_profile_renders_bundle_variables():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    for t in ("demo", "dev", "staging", "prod"):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "bundle_vars.py"), "--target", t], capture_output=True, text=True)
        assert r.returncode == 0, (t, r.stderr[-400:])

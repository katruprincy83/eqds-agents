"""The app requirements are fully pinned lock files, so the Apps builder never has to solve transitive versions (pip backtracking
timed out the specialist deploys). The locks must stay in step with PINS / REQ in scripts/build_apps.py."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_apps  # noqa: E402


def test_locks_match_pins():
    r = subprocess.run([sys.executable, str(ROOT / "scripts/lock_requirements.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_line_is_an_exact_pin():
    for kind in build_apps.REQ:
        for line in (ROOT / "requirements" / f"{kind}.lock").read_text().splitlines():
            if line and not line.startswith("#"):
                assert "==" in line and not any(c in line for c in "<>~!"), f"{kind}.lock: {line}"


def test_build_uses_the_lock():
    text = build_apps.requirements_text("specialist")
    assert text == (ROOT / "requirements/specialist.lock").read_text()
    assert "langsmith==" in text  # a transitive package: only present in the lock

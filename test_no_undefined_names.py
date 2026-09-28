"""No undefined name reaches production again.

WHY. The reconciliation endpoint shipped referencing `_dt`, which did not
exist in that module. Every unit test passed - they exercise the pure
functions, not the endpoint's own namespace - and the only thing that
caught it was the live deploy answering:

    reconciliation could not run: NameError: name '_dt' is not defined

It reported itself cleanly, because the pass before this one made every
UNKNOWN carry its cause. But a deploy is a slow, expensive place to learn
that a name is missing, and the check is free.

This is the same class as everything else found over 2026-09-27/28: two
things that must agree - here a reference and a binding - with nothing
forcing them to match until runtime.

Scoped to the modules this work actually touches. A repo-wide sweep would
fail on pre-existing noise elsewhere and get muted, and a muted check is
worse than no check.
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

MODULES = [
    "routers/trading_dashboard.py",
    "crypto_grid_bot.py",
    "allocation_backing.py",
    "reconcile.py",
    "invariants.py",
    "capital_velocity.py",
    "coin_scan.py",
    "coin_rotation.py",
    "step_study.py",
    "fee_floor.py",
]


def undefined_names(path):
    try:
        out = subprocess.run([sys.executable, "-m", "pyflakes", os.path.join(HERE, path)],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        pytest.skip(f"pyflakes unavailable: {e}")
    if "No module named" in (out.stderr or ""):
        pytest.skip("pyflakes not installed")
    return [l for l in (out.stdout or "").splitlines() if "undefined name" in l]


@pytest.mark.parametrize("path", MODULES)
def test_module_has_no_undefined_names(path):
    if not os.path.exists(os.path.join(HERE, path)):
        pytest.skip(f"{path} not present")
    bad = undefined_names(path)
    assert not bad, "\n".join(bad)


def test_the_check_can_actually_fail(tmp_path):
    """A test that cannot fail is worse than no test. Prove pyflakes really
    reports the exact defect that shipped, on a file that reproduces it."""
    f = tmp_path / "boom.py"
    f.write_text("def go():\n    return _dt.utcnow()\n")
    out = subprocess.run([sys.executable, "-m", "pyflakes", str(f)],
                         capture_output=True, text=True, timeout=60)
    if "No module named" in (out.stderr or ""):
        pytest.skip("pyflakes not installed")
    assert "undefined name" in out.stdout
    assert "_dt" in out.stdout

"""Fail-soft parsing for numeric environment variables.

Why this module exists
----------------------
Trading modules read their tuning knobs at import time::

    MAKER_ONLY_ORDER_WAIT_SECONDS = int(os.getenv("GRID_MAKER_ONLY_WAIT_SECONDS", "240"))

That line runs while the module is being imported. If the variable holds
anything `int()` cannot parse, the exception propagates out of the import,
the module never loads, and every caller of it dies with it. On 2026-10-04 a
single variable set to the literal text ``240 -> 3600`` (a recommendation
pasted verbatim instead of the number) took down ``/grid-status``,
``/trading-profile``, the rotation/truth/adopt/idle passes and - worst of all -
left the stop layer running blind, logging "could not read grid positions ...
placing without that protection this pass".

A mistyped tuning knob must never be able to do that. These helpers fall back
to the declared default, log loudly, and record the fallback so the dashboard
can show it. The alternative - a hard crash - protects nothing: it does not
keep the bad value out of the system, it just removes the system.

Fail-soft, not fail-silent
--------------------------
A silent fallback is its own hazard: the owner sets 3600, the parse fails, the
bot quietly runs at 240, and nothing anywhere says so. Every fallback is
therefore recorded in ``ENV_PARSE_FALLBACKS`` and surfaced by
``/api/trading-dashboard/module-health``.

This module deliberately has no imports beyond the standard library and must
stay dependency-free, so that it can never itself become an import-time
failure.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("env_config")

# name -> {"raw": <bounded repr of the bad value>, "default": <value used>,
#          "kind": "int"|"float", "error": <str>}
ENV_PARSE_FALLBACKS: dict[str, dict[str, Any]] = {}

# A malformed knob is config, not a credential, but bound it anyway so a
# variable that turns out to hold something long never lands in a log or an
# HTTP body in full.
_MAX_RAW_CHARS = 60


def _bounded(raw: str) -> str:
    raw = raw.strip()
    if len(raw) <= _MAX_RAW_CHARS:
        return raw
    return raw[:_MAX_RAW_CHARS] + "..."


def _record(name: str, raw: str, default: Any, kind: str, error: str) -> None:
    ENV_PARSE_FALLBACKS[name] = {
        "raw": _bounded(raw),
        "default": default,
        "kind": kind,
        "error": error,
    }
    log.warning(
        "ENV PARSE FAILED: %s=%r is not a valid %s (%s). "
        "Using the default %r instead - the value you set is NOT in effect. "
        "Fix the variable to digits only.",
        name, _bounded(raw), kind, error, default,
    )


def env_int(name: str, default: int) -> int:
    """int(os.getenv(name)) that can never break an import.

    Tolerates surrounding whitespace and a value written as a float with no
    fractional part ("3600.0"). Anything else falls back to `default`.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    text = raw.strip()
    try:
        return int(text)
    except (TypeError, ValueError) as int_exc:
        int_error = str(int_exc)
    try:
        as_float = float(text)
    except (TypeError, ValueError):
        # Report the int error, not the float one - int is what was asked for.
        _record(name, raw, default, "int", int_error)
        return default
    if as_float.is_integer():
        return int(as_float)
    _record(name, raw, default, "int", "not a whole number")
    return default


def env_float(name: str, default: float) -> float:
    """float(os.getenv(name)) that can never break an import."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except (TypeError, ValueError) as exc:
        _record(name, raw, default, "float", str(exc))
        return default


def env_fallback_report() -> dict[str, Any]:
    """What the dashboard shows so a silent fallback cannot stay silent."""
    items = [
        {
            "variable": name,
            "bad_value": info["raw"],
            "expected": info["kind"],
            "running_with_default": info["default"],
            "error": info["error"],
        }
        for name, info in sorted(ENV_PARSE_FALLBACKS.items())
    ]
    if items:
        note = (
            f"{len(items)} environment variable(s) could not be parsed and are "
            "NOT in effect; the built-in defaults are running instead. Set each "
            "to a bare number (digits only, no arrows, no units, no quotes)."
        )
    else:
        note = "All numeric environment variables parsed cleanly."
    return {"count": len(items), "fallbacks": items, "note": note}

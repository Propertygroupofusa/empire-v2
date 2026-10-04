"""Catch a coin being delisted or halted BEFORE it costs an order.

WHY THIS EXISTS, from this account's own history. JUP-USD returned
"INVALID_ARGUMENT: Invalid product_id" on every single buy attempt,
repeatedly, across multiple sessions, before anyone worked out it was simply
no longer a listed product - and while that was unknown, branches kept
landing on it. MATIC-USD was a real Coinbase migration to POL-USD, and a
branch sat stuck on the dead ticker. Both are venue facts that were knowable
the moment they happened.

WHY NOT A NEWS API. A news feed reports a delisting after a journalist writes
it up. The exchange's own product list changes at the moment it becomes true,
costs nothing, needs no key, and cannot be wrong about its own catalogue. So
this watches the catalogue, not the coverage.

PURE AND READ-ONLY. This module fetches nothing, holds no credentials and
places no order. It takes two snapshots and returns alert dicts in exactly
the shape alert_worker already writes (kind/asset/severity/message/detail/
dedupe_key). The caller owns the fetching and the storing.

THE DANGEROUS FAILURE MODE, and the rule that prevents it: a snapshot that
could not be read is NOT a delisting. One timeout against the venue would
otherwise look identical to "every coin you own was removed at once" and
would fire a CRITICAL alert per branch. An empty or missing `current` returns
NOTHING - the same unknown-is-not-zero doctrine the backing check and the
realized/unrealized split already follow.
"""

ONLINE_STATUSES = frozenset({"online", "ONLINE", "active", "ACTIVE"})

KIND_DELISTED = "DELISTED"
KIND_HALTED = "TRADING_HALTED"
KIND_RESUMED = "TRADING_RESUMED"
KIND_NEW = "NEW_LISTING"


def _norm(product_id) -> str:
    return str(product_id or "").strip().upper()


def is_tradeable(entry) -> bool:
    """True when the venue says this product can be traded right now.

    Treated as tradeable unless something explicitly says otherwise, so a
    field this account's API tier does not return cannot invent a halt.
    """
    if not isinstance(entry, dict):
        return True
    if entry.get("trading_disabled") is True:
        return False
    if entry.get("is_disabled") is True:
        return False
    if entry.get("cancel_only") is True or entry.get("post_only") is True:
        return False
    status = entry.get("status")
    if status is None:
        return True
    return str(status) in ONLINE_STATUSES


def plan_alerts(previous: dict, current: dict, *, watched=(), quote: str = "USD") -> list:
    """Alert dicts for what changed between two product snapshots.

    `previous` / `current` map product_id -> the venue's product dict.
    `watched` are the products this fleet actually cares about; only those
    can raise a CRITICAL. A new listing anywhere on the venue is INFO.
    """
    # A READ THAT FAILED IS NOT AN EVENT. Without this, one timeout reports
    # the whole fleet delisted.
    if not current:
        return []

    cur = {_norm(k): v for k, v in current.items()}
    prev = {_norm(k): v for k, v in (previous or {}).items()}
    watch = {_norm(w) for w in (watched or ())}
    out = []

    for pid in sorted(watch):
        was_there = pid in prev
        now_there = pid in cur
        if was_there and not now_there:
            out.append({
                "kind": KIND_DELISTED, "asset": pid, "severity": "CRITICAL",
                "message": f"{pid} is gone from the venue's product list",
                "detail": ("It was listed on the previous reading and is absent now. "
                           "A branch on this coin cannot buy or sell it - every order "
                           "will be refused, the way JUP-USD was."),
                "dedupe_key": f"listing:{KIND_DELISTED}:{pid}",
            })
            continue
        if not now_there:
            continue
        tradeable_now = is_tradeable(cur[pid])
        tradeable_before = is_tradeable(prev[pid]) if was_there else True
        if tradeable_before and not tradeable_now:
            out.append({
                "kind": KIND_HALTED, "asset": pid, "severity": "CRITICAL",
                "message": f"{pid} is listed but not currently tradeable",
                "detail": (f"The venue reports status={cur[pid].get('status')!r}, "
                           f"trading_disabled={cur[pid].get('trading_disabled')!r}. "
                           "Orders on this coin will be refused until it resumes."),
                "dedupe_key": f"listing:{KIND_HALTED}:{pid}",
            })
        elif was_there and not tradeable_before and tradeable_now:
            out.append({
                "kind": KIND_RESUMED, "asset": pid, "severity": "INFO",
                "message": f"{pid} is tradeable again",
                "detail": "The venue is accepting orders on this product again.",
                "dedupe_key": f"listing:{KIND_RESUMED}:{pid}",
            })

    # A FIRST RUN HAS NOTHING TO COMPARE AGAINST. Announcing all several
    # hundred products as "new" would bury the two lines that matter.
    if prev:
        suffix = f"-{_norm(quote)}"
        for pid in sorted(set(cur) - set(prev)):
            if not pid.endswith(suffix):
                continue
            out.append({
                "kind": KIND_NEW, "asset": pid, "severity": "INFO",
                "message": f"{pid} is newly listed",
                "detail": "It appeared on the venue's product list since the last reading.",
                "dedupe_key": f"listing:{KIND_NEW}:{pid}",
            })
    return out


def snapshot_from_products(products) -> dict:
    """Reduce a venue product payload to just what this module compares, so a
    stored snapshot stays small and a field rename cannot silently widen it."""
    out = {}
    for p in (products or []):
        if not isinstance(p, dict):
            continue
        pid = _norm(p.get("product_id") or p.get("id"))
        if not pid:
            continue
        out[pid] = {
            "status": p.get("status"),
            "trading_disabled": p.get("trading_disabled"),
            "is_disabled": p.get("is_disabled"),
            "cancel_only": p.get("cancel_only"),
            "post_only": p.get("post_only"),
        }
    return out

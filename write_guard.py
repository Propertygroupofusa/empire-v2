"""Nothing changes state from a URL without the token. Fail closed.

WHY

empire-v2's trading endpoints are public and unauthenticated. There are 110
POST routes under /api/trading-dashboard alone, and several of them move
real money: /grid-status/force-buy places a market order, /quick-buy opens a
funded branch, /withdraw-grid moves cash, /grid-status/mode stops the fleet
trading. On 2026-09-26 a $10 BTC order was placed with one curl and no
credential of any kind. Anyone holding the Railway URL could do the same,
and the account it reaches holds $11,121.

DESIGN, AND WHY IT IS MIDDLEWARE

Not a decorator on each route. 110 decorators is 110 chances to forget one,
and the route that gets forgotten is the one that is added next month by
someone who never read this file. A request-level check cannot be skipped by
omission - a new route is protected the moment it exists.

FAIL CLOSED, DELIBERATELY

With no token configured, every write is REFUSED rather than allowed. The
opposite default - protect only once a variable is set - is protection that
silently does nothing, and this deployment has a history of environment
variables that would not take: CRYPTO_STRATEGY_MODE was corrected six times
through the Railway UI on 2026-09-25 and kept reading the old value. A
guard that depends on a variable landing is not a guard.

The cost of failing closed is understood: until DASHBOARD_WRITE_TOKEN is
set, the dashboard's buttons stop working. Nothing else does. The trading
loops run in-process and never call these endpoints over HTTP - verified,
no module in this repository issues a request to its own API - so refusing
writes cannot stop the fleet trading or interrupt an open position.

WHAT IS NOT COVERED

Reads. /account-census and /coinbase/balances still return the full holdings
to anyone with the URL. That is a real exposure and it is NOT fixed here:
the dashboard runs in a browser that cannot send a secret header, so
protecting reads needs a decision about how the page authenticates. Writes
are the part where someone else can spend the money, so writes go first.
"""
from __future__ import annotations

import hmac
import logging
import os

log = logging.getLogger("write_guard")

TOKEN_ENV = "DASHBOARD_WRITE_TOKEN"
HEADER = "x-dashboard-token"
QUERY = "token"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Path prefixes that require the token. Everything that can move money or
# change what the bots do.
PROTECTED_PREFIXES = (
    "/api/trading-dashboard",
    "/api/crypto-trading",
    "/api/alpaca-funding",
    "/api/sweep",
    "/api/orders",
    "/api/payments",
    "/api/admin",
)

# There is deliberately NO exemption list.
#
# The first draft had one - login, register, refresh - and every entry was
# dead code, because /api/auth is not under a protected prefix to begin
# with. An allowlist whose entries can never fire is worse than none: it
# reads as a considered carve-out and protects nothing, and the next person
# adds a fourth line to it believing that means something.
#
# /api/auth is out of scope by design, not by exemption. It is a different
# authentication domain: those routes establish a USER's identity, and a
# shared deployment token in front of login would lock everyone out of the
# thing that issues credentials. If they need protection it is their own
# session logic, not this.


def _configured_token() -> str:
    return (os.getenv(TOKEN_ENV) or "").strip()


def _presented(request) -> str:
    v = request.headers.get(HEADER)
    if v:
        return v.strip()
    # Query fallback so a terminal can use it. Header is preferred: a query
    # string lands in access logs and browser history.
    return (request.query_params.get(QUERY) or "").strip()


def is_protected(method: str, path: str) -> bool:
    if method.upper() not in MUTATING:
        return False
    return any(path.startswith(p) for p in PROTECTED_PREFIXES)


def check(method: str, path: str, presented: str):
    """Returns None to allow, or (status, detail) to refuse."""
    if not is_protected(method, path):
        return None
    token = _configured_token()
    if not token:
        return (503, (
            f"This deployment has no {TOKEN_ENV} set, so every state-changing "
            f"request is refused. Set {TOKEN_ENV} to a long random string in the "
            f"environment, then send it as the {HEADER} header. Reads are "
            f"unaffected. Failing closed is deliberate - an unset guard that "
            f"allowed writes would protect nothing while appearing to."))
    if not presented:
        return (401, f"Missing {HEADER} header (or ?{QUERY}=). This endpoint changes state.")
    if not hmac.compare_digest(presented, token):
        return (403, f"The {HEADER} presented does not match this deployment's token.")
    return None


async def guard(request, call_next):
    """Starlette middleware. Refuses unauthenticated writes.

    The response class is imported HERE, not at module scope, so the policy
    above - is_protected and check - can be exercised without a web
    framework installed. A guard whose tests need the whole server to boot
    is a guard that stops being tested.
    """
    from starlette.responses import JSONResponse
    verdict = check(request.method, request.url.path, _presented(request))
    if verdict is not None:
        status, detail = verdict
        log.warning(
            "[GUARD] refused %s %s (%s) from %s", request.method, request.url.path,
            status, getattr(request.client, "host", "?"))
        return JSONResponse({"detail": detail, "guard": "write_guard"}, status_code=status)
    return await call_next(request)

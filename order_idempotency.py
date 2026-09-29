"""A client_order_id derived from the order's intent, not from randomness.

THE FINDING THIS EXISTS FOR. Every order path in this repository builds its
client_order_id as `str(uuid.uuid4())` - ten call sites, checked. That field
is the VENUE'S OWN duplicate-protection key: Coinbase treats a repeated
client_order_id as the same order and returns the original rather than
opening a second. Randomising it per attempt throws that protection away.
The system currently has no duplicate protection at all, at any layer, and
a retry or a twice-delivered event creates a genuinely new position.

§23 asks for an idempotency key of cycle + slice + side + target price +
quantity. Making that key the client_order_id is strictly stronger than
checking a local table before submitting: it survives a process restart, it
survives two processes racing (the polling loop and an event loop both
deciding to place the same order), and it needs no database read on the hot
path. The check happens at the venue, which is the only place that can
actually see both attempts.

WHY `attempt` IS REQUIRED AND NOT OPTIONAL. Pure determinism over
(cycle, slice, side, price, quantity) would mean an order legitimately
cancelled and re-placed at the same price could never be sent again - the
venue would keep returning the cancelled one. So the caller passes an
explicit attempt number. That is the whole distinction this module draws:
an ACCIDENTAL duplicate reuses the same attempt and is refused by the
venue; a DELIBERATE re-placement increments it and is a new order. A random
id cannot tell those apart, which is exactly the present behaviour.

THE PREFIX IS LOAD-BEARING. resting_stops_worker writes "rstop-" and
free_locked_inventory filters on it to cancel only the stops this system
placed - a stop the owner set by hand is left alone. So the prefix survives
at the front of the id, unhashed, and a prefix that will not fit refuses
rather than being silently truncated.

NOTHING HERE IS RANDOM OR TIME-DEPENDENT. A test asserts by AST that this
module imports neither uuid, random nor time: an id that varies between two
calls with the same intent is not an idempotency key, however it is
spelled.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation

# Coinbase accepts a client_order_id up to 36 characters - the length of the
# UUID every existing call site sends. Staying inside what is already known
# to work rather than probing for the real ceiling on a live account.
MAX_LENGTH = 36
# Below this the collision margin stops being comfortable. 16 hex chars is
# 64 bits; two intents colliding would need ~4 billion orders.
MIN_DIGEST = 16


def _canon_number(v):
    """A number's canonical text, so 100.10 and 100.1 are one intent.

    They ARE one intent - the same price written two ways - and an id that
    differed between them would let the same order through twice. But 100.1
    and 100.2 must differ, so this normalises without rounding.
    """
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    d = d.normalize()
    # normalize() can produce exponent form (1E+2); :f never does, so two
    # spellings of one value cannot hash differently.
    return f"{d:f}"


def _canon_text(v):
    if v is None:
        return None
    s = str(v).strip()
    return s.upper() if s else None


def intent_key(*, cycle_id, slice_id, side, product_id, target_price,
               quantity, attempt):
    """The canonical text of one order intent, or None if incomplete.

    None for ANY field returns None for the whole key. A key built from a
    partial intent would collide with a different partial intent - two
    orders that differ only in the field that was missing would share an
    id, and the venue would silently drop the second.
    """
    parts = [
        _canon_text(cycle_id), _canon_text(slice_id), _canon_text(side),
        _canon_text(product_id), _canon_number(target_price),
        _canon_number(quantity), _canon_number(attempt),
    ]
    if any(p is None for p in parts):
        return None
    # A separator that cannot appear in any canonicalised field, so
    # ("ab","c") and ("a","bc") cannot produce one key.
    return "|".join(parts)


def client_order_id(*, prefix="", cycle_id=None, slice_id=None, side=None,
                    product_id=None, target_price=None, quantity=None,
                    attempt=None):
    """The id to send to the venue, or None if the intent is incomplete.

    None means DO NOT SUBMIT. Falling back to a random id here would
    restore exactly the behaviour this module exists to remove, and it
    would do so precisely when something was already wrong.
    """
    key = intent_key(cycle_id=cycle_id, slice_id=slice_id, side=side,
                     product_id=product_id, target_price=target_price,
                     quantity=quantity, attempt=attempt)
    if key is None:
        return None
    pre = "" if prefix is None else str(prefix)
    budget = MAX_LENGTH - len(pre)
    if budget < MIN_DIGEST:
        # Refusing rather than truncating the prefix: free_locked_inventory
        # matches on it to decide whether an order is ours to cancel, and a
        # shortened prefix would make this system's own order invisible to
        # its own cleanup.
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:budget]
    return pre + digest


def same_intent(a, b):
    """Whether two intent dicts describe the same order.

    Used to answer "have I already sent this?" without a venue round trip.
    Two incomplete intents are never 'the same' - unknown is not a match.
    """
    ka = intent_key(**a) if isinstance(a, dict) else None
    kb = intent_key(**b) if isinstance(b, dict) else None
    if ka is None or kb is None:
        return False
    return ka == kb

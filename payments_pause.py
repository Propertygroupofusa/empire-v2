"""
Global kill switch for collecting new payments.

Set PAYMENTS_PAUSED=true in the environment to stop every checkout/payment
creation path from calling Stripe (e.g. while a Stripe account review has
live charges blocked) and return a clear "temporarily unavailable" response
instead of a Stripe error. Existing webhooks, refunds, and admin/read
endpoints are unaffected. Set back to false (or unset) once Stripe clears.
"""

import os

PAUSE_MESSAGE = (
    "Payments are temporarily paused while our account completes a Stripe "
    "review. Please check back soon."
)


def payments_paused() -> bool:
    return os.getenv("PAYMENTS_PAUSED", "false").strip().lower() == "true"

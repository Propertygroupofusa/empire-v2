"""One place that says which model the app's AI features call.

WHY THIS EXISTS

On 2026-10-04 the Daily Ventures Brief failed with:

    POST https://api.anthropic.com/v1/messages "HTTP/1.1 404 Not Found"
    {'type': 'not_found_error', 'message': 'model: claude-3-5-sonnet-20241022'}

That id had been retired by the API. The brief still logged "Daily brief
generated, persisted, and sent" immediately afterwards, so from the outside
the feature looked alive while producing nothing at all.

The id was hardcoded at 13 call sites across 9 files, which is why it was
still there long after it stopped working: a retirement has to be chased
through nine files, and nobody notices eight of them. Now there is one
constant, overridable from the environment, so the next retirement is one
variable rather than a sweep.

Parsed through env_config-style tolerance is not needed here - this is a
string, not a number - but the same lesson applies: a blank or whitespace
value falls back to the default rather than sending an empty model id.
"""

import os

# The id the app calls. Same capability tier as the retired one it replaces.
# Override with CLAUDE_TEXT_MODEL to move every feature at once; set it to a
# bare id with no quotes and no surrounding spaces.
_DEFAULT_TEXT_MODEL = "claude-sonnet-5-5"


def text_model() -> str:
    """The model id for the app's text-generation features."""
    raw = os.getenv("CLAUDE_TEXT_MODEL")
    if raw and raw.strip():
        return raw.strip()
    return _DEFAULT_TEXT_MODEL


# Module-level convenience for call sites that read it once at import.
TEXT_MODEL = text_model()

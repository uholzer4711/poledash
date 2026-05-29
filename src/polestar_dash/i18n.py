"""Lightweight UI internationalization.

Dict-based, no build step (Babel/gettext would add a compile step the repo
forbids). English source strings are the lookup keys, so a missing German
entry degrades gracefully to English instead of crashing, and templates stay
self-documenting (`{{ t("Live telemetry") }}`).

Language is a single app-wide setting (no auth, single-user self-hosted), so
we cache it in a module global and refresh on `set_language`. This lets the
notification/email code — which has no request context — translate too.
"""

from __future__ import annotations

import logging

from .translations_de import TRANSLATIONS_DE

log = logging.getLogger(__name__)

LANG_KEY = "ui_language"
DEFAULT_LANG = "de"
SUPPORTED_LANGS = ("de", "en")

# A handful of accent-word headings are rendered with |safe and a single
# whole-phrase key (so German compounds like "Ladevorgänge" aren't split mid
# word). In English we still want the orange accent on one word, so override
# just those keys; every other English string falls back to the key verbatim.
TRANSLATIONS_EN = {
    "Charging sessions": 'Charging <em class="accent">sessions</em>',
    "Charging curve": 'Charging <em class="accent">curve</em>',
    "Last {n} days": 'Last <em class="accent">{n}</em> days',
}

# English source strings → translated. "en" only needs the accent overrides;
# any key absent from its catalog falls through to the key verbatim.
_CATALOGS = {"de": TRANSLATIONS_DE, "en": TRANSLATIONS_EN}

_lang_cache: str | None = None


def get_language() -> str:
    global _lang_cache
    if _lang_cache is None:
        from .app_settings import get_setting

        val = get_setting(LANG_KEY, DEFAULT_LANG)
        _lang_cache = val if val in SUPPORTED_LANGS else DEFAULT_LANG
    return _lang_cache


def set_language(lang: str) -> None:
    global _lang_cache
    if lang not in SUPPORTED_LANGS:
        raise ValueError(f"Unsupported language: {lang!r}")
    from .app_settings import set_setting

    set_setting(LANG_KEY, lang)
    _lang_cache = lang


def t(text: str, /, **kwargs: object) -> str:
    """Translate `text` into the active language. `text` is the English
    source string. Pass `{name}`-style placeholders as kwargs."""
    lang = get_language()
    catalog = _CATALOGS.get(lang)
    out = catalog.get(text, text) if catalog else text
    if kwargs:
        try:
            return out.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            # A malformed placeholder must never take down a page render.
            log.warning("i18n format failed for %r", text)
            return out
    return out

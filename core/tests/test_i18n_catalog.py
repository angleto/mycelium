"""Catalog completeness guards (docs/adr/0017).

Pure unit tests: no DB, no settings.

``render`` degrades gracefully (missing locale -> English, missing
English template -> the bare ``code.value``), which is right at
runtime but hides gaps: ``MessageCode.ADJUDICATION_NOT_FOUND`` shipped
with no English entry and users hitting that path were shown the
literal string ``adjudication.not_found``. Nothing in the suite
noticed. These tests close that class of gap.

The two locales are NOT symmetric on purpose. ``en`` is the reference
table and must be total over ``MessageCode``; ``it`` is deliberately
partial (only the strings the backend actually emits to a recipient,
currently the reminder notifications) and leans on the English
fallback, as ``i18n._CATALOG`` documents. So completeness is asserted
on the default locale, and what is asserted for every locale is the
property users actually care about: no code ever renders as its own
identifier.
"""

from __future__ import annotations

import string

import pytest

from mycelium_core.i18n import _CATALOG, DEFAULT_LOCALE, MessageCode, render

LOCALES = sorted(_CATALOG)


def _placeholders(template: str) -> set[str]:
    """Named ``str.format`` fields used by a template."""
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_default_locale_covers_every_message_code() -> None:
    """Every enum member has a template in the reference locale.

    This is the assertion that makes a missing entry impossible to
    reintroduce: ``render`` falls back to ``DEFAULT_LOCALE``, so a hole
    here is a hole for every locale.
    """
    missing = sorted(c.value for c in MessageCode if c not in _CATALOG[DEFAULT_LOCALE])
    assert not missing, (
        f"MessageCode members with no _CATALOG[{DEFAULT_LOCALE!r}] entry "
        f"({len(missing)}): {', '.join(missing)}"
    )


@pytest.mark.parametrize("locale", LOCALES)
def test_no_code_renders_as_its_own_identifier(locale: str) -> None:
    """No user is ever shown a raw code such as ``adjudication.not_found``.

    Holds for the partial locales too, via the English fallback in
    ``render``.
    """
    bare = sorted(c.value for c in MessageCode if render(c, locale) == c.value)
    assert not bare, (
        f"codes rendering as their own identifier in locale {locale!r} "
        f"({len(bare)}): {', '.join(bare)}"
    )


@pytest.mark.parametrize("locale", [loc for loc in LOCALES if loc != DEFAULT_LOCALE])
def test_translation_is_a_subset_of_the_reference_locale(locale: str) -> None:
    """A translated table may be partial, but never invent codes.

    Catches an entry left behind after a code is dropped or renamed.
    """
    orphans = sorted(c.value for c in _CATALOG[locale] if c not in _CATALOG[DEFAULT_LOCALE])
    assert not orphans, (
        f"codes present in locale {locale!r} but absent from "
        f"{DEFAULT_LOCALE!r} ({len(orphans)}): {', '.join(orphans)}"
    )


@pytest.mark.parametrize("locale", [loc for loc in LOCALES if loc != DEFAULT_LOCALE])
def test_translation_placeholders_match_the_reference_locale(locale: str) -> None:
    """Translations keep the same ``{named}`` fields as the English one.

    ``render`` swallows ``KeyError``/``IndexError`` and returns the raw
    template, so an invented placeholder would surface to the user as
    literal braces, and a dropped one would silently lose information.
    """
    mismatched = sorted(
        f"{code.value} (expected {sorted(_placeholders(_CATALOG[DEFAULT_LOCALE][code]))}, "
        f"got {sorted(_placeholders(template))})"
        for code, template in _CATALOG[locale].items()
        if code in _CATALOG[DEFAULT_LOCALE]
        and _placeholders(template) != _placeholders(_CATALOG[DEFAULT_LOCALE][code])
    )
    assert not mismatched, (
        f"placeholder drift in locale {locale!r} ({len(mismatched)}): {'; '.join(mismatched)}"
    )

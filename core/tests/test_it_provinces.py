"""Italian province validation for FatturaPA Provincia (#3)."""

from __future__ import annotations

from mycelium_core.it_provinces import is_valid_provincia


def test_real_italian_province_accepted() -> None:
    assert is_valid_provincia("RM", "IT")
    assert is_valid_provincia("mi", "IT")  # case-insensitive


def test_fake_province_rejected_for_italy() -> None:
    assert not is_valid_provincia("XX", "IT")
    assert not is_valid_provincia("ZZ", None)  # absent country defaults to IT


def test_empty_or_foreign_is_accepted() -> None:
    assert is_valid_provincia(None, "IT")
    assert is_valid_provincia("", "IT")
    assert is_valid_provincia("XX", "FR")  # foreign: not validated here

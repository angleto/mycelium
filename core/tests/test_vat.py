"""VAT id normalization for FatturaPA IdFiscaleIVA (issuer + client)."""

from __future__ import annotations

from mycelium_core.vat import is_valid_vat_code, normalize_vat


def test_strips_country_prefix_into_country() -> None:
    assert normalize_vat("IT01112223334", "IT") == ("IT", "01112223334")
    assert normalize_vat("it 01112223334", "IT") == ("IT", "01112223334")  # spaces + case
    # The prefix wins over the passed country (it is part of the id).
    assert normalize_vat("DE123456789", "IT") == ("DE", "123456789")


def test_bare_code_is_kept_with_given_country() -> None:
    assert normalize_vat("01112223334", "IT") == ("IT", "01112223334")
    assert normalize_vat(None, "IT") == ("IT", None)


def test_validation_requires_11_digits_for_italy() -> None:
    assert is_valid_vat_code("01112223334", "IT")
    assert not is_valid_vat_code("IT01112223334", "IT")  # prefix not stripped -> invalid
    assert not is_valid_vat_code("123", "IT")
    assert not is_valid_vat_code("1343881001X", "IT")
    # Empty is accepted here (presence enforced by the invoice validation).
    assert is_valid_vat_code(None, "IT")
    # Foreign formats are not validated here (post-v1).
    assert is_valid_vat_code("123456789", "DE")

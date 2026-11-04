"""E.164, and the rule that a number is never guessed: a wrong guess messages a
stranger. Every number here is one nobody can have."""

import pytest

from harvey.whatsapp.numbers import country_of_number, dial_code_of, normalize_phone


@pytest.mark.parametrize("raw,country,expected", [
    ("+1 (415) 555-0100", "", "+14155550100"),
    ("+14155550100", "US", "+14155550100"),
    ("tel:+1-415-555-0100", "", "+14155550100"),
    ("+44 7700 900123", "", "+447700900123"),
    ("+44 (0)7700 900123", "", "+447700900123"),   # the trunk 0 written after the dial code
    ("+44\u00a07700\u00a0900123", "", "+447700900123"),
    ("+91 12345 67890", "", "+911234567890"),
    # No +: the country gives the dial code.
    ("(415) 555-0100", "US", "+14155550100"),
    ("1 415 555 0100", "US", "+14155550100"),
    ("07700 900123", "GB", "+447700900123"),
    ("7700 900123", "GB", "+447700900123"),
    ("447700900123", "GB", "+447700900123"),       # the dial code without its +
    ("12345 67890", "IN", "+911234567890"),
    ("0123 456 7890", "IN", "+911234567890"),
    ("911234567890", "IN", "+911234567890"),
    ("0491 570 156", "AU", "+61491570156"),
])
def test_a_number_whose_country_code_is_certain(raw, country, expected):
    assert normalize_phone(raw, country).e164 == expected


@pytest.mark.parametrize("raw,country", [
    ("(415) 555-0100", ""),            # no + and no country: can't be established
    ("07700 900123", ""),
    ("415 555 0100", "XX"),            # a country nobody knows
    ("11 1234 5678", "AR"),            # a country whose local format isn't read (needs a +)
    ("0044 7700 900123", "GB"),        # 00 means different things in different countries
    ("00 1 415 555 0100", "US"),
    ("4912345678", "DE"),              # national, or +49 without the +: both fit
    ("555 0100", "US"),                # too short
    ("415 055 0100", "US"),            # no exchange starts with 0
    ("015 555 0100", "US"),            # no area code starts with 0
    ("+999 123 456 789", ""),          # no such country code
    ("+800 1234 5678", ""),            # a non-geographic code is nobody's chat
    ("+1 555 0100", ""),               # too short for +1
    ("+44 123", ""),
    ("415-555-0100 x12", "US"),        # an extension isn't part of the number
    ("call me", "US"),
    ("+1 415 555 0100, +1 415 555 0101", ""),
    ("", "US"),
])
def test_anything_less_is_never_guessed(raw, country):
    number = normalize_phone(raw, country)
    assert not number.ok and number.e164 == "" and number.problem


def test_a_plus_number_wins_over_the_country_given():
    assert normalize_phone("+44 7700 900123", "US").e164 == "+447700900123"


def test_the_country_a_number_names():
    assert dial_code_of("447700900123") == "44" and dial_code_of("14155550100") == "1"
    assert country_of_number("+447700900123") == "GB"
    assert country_of_number("+911234567890") == "IN"
    assert country_of_number("+14155550100") == ""     # shared: the US, Canada, the Caribbean
    assert country_of_number("") == ""

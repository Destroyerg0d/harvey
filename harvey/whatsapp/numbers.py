"""Phone numbers as E.164, and the rule that a number is never guessed.

A wrong guess messages a stranger, so a number is only accepted when its
country code is certain: it was written with a leading + (or ``tel:+``), or
the country is known and the digits fit how numbers are written there. Anything
else comes back with a problem, and the contact waits for the owner as
``needs_review``. Nothing here tries to be clever about a number it isn't sure of.
"""

import re
import unicodedata
from dataclasses import dataclass

# How numbers are written in a country: (dial code, national trunk prefix,
# lengths of the national number without the trunk prefix). A country missing
# here isn't an error: its numbers need a + to be accepted, because the local
# format can't be read with certainty (Argentina's mobile 9, for one).
_NANP = ("1", "", (10,))
COUNTRIES: dict[str, tuple[str, str, tuple[int, ...]]] = {
    "US": _NANP, "CA": _NANP,
    "GB": ("44", "0", (9, 10)), "IE": ("353", "0", (7, 8, 9)),
    "AU": ("61", "0", (9,)), "NZ": ("64", "0", (8, 9, 10)),
    "IN": ("91", "0", (10,)), "PK": ("92", "0", (9, 10)), "BD": ("880", "0", (10,)),
    "LK": ("94", "0", (9,)), "SG": ("65", "", (8,)), "HK": ("852", "", (8,)),
    "MY": ("60", "0", (9, 10)), "ID": ("62", "0", (9, 10, 11, 12)), "PH": ("63", "0", (10,)),
    "TH": ("66", "0", (8, 9)), "VN": ("84", "0", (9, 10)), "JP": ("81", "0", (9, 10)),
    "KR": ("82", "0", (9, 10)), "CN": ("86", "0", (10, 11)), "TW": ("886", "0", (8, 9)),
    "AE": ("971", "0", (8, 9)), "SA": ("966", "0", (8, 9)), "IL": ("972", "0", (8, 9)),
    "TR": ("90", "0", (10,)), "EG": ("20", "0", (9, 10)), "ZA": ("27", "0", (9,)),
    "NG": ("234", "0", (8, 10)), "KE": ("254", "0", (9,)), "GH": ("233", "0", (9,)),
    "DE": ("49", "0", tuple(range(7, 13))), "FR": ("33", "0", (9,)), "ES": ("34", "", (9,)),
    "IT": ("39", "", tuple(range(6, 12))), "PT": ("351", "", (9,)), "NL": ("31", "0", (9,)),
    "BE": ("32", "0", (8, 9)), "CH": ("41", "0", (9,)), "AT": ("43", "0", tuple(range(7, 14))),
    "SE": ("46", "0", (7, 8, 9)), "NO": ("47", "", (8,)), "DK": ("45", "", (8,)),
    "FI": ("358", "0", tuple(range(6, 11))), "PL": ("48", "", (9,)), "CZ": ("420", "", (9,)),
    "GR": ("30", "", (10,)), "HU": ("36", "06", (8, 9)), "RO": ("40", "0", (9,)),
    "UA": ("380", "0", (9,)), "RU": ("7", "8", (10,)),
    "MX": ("52", "", (10,)), "BR": ("55", "0", (10, 11)), "CL": ("56", "", (9,)),
    "CO": ("57", "", (10,)), "PE": ("51", "", (9,)),
}
# Italy keeps the 0 of a landline inside the international number.
_KEEPS_LEADING_ZERO = {"IT"}

# Every geographic country calling code. Non-geographic ones (800, 870, 881...)
# are left out on purpose: no person's chat lives there.
_DIAL_CODES = frozenset(
    {"1", "7", "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44",
     "45", "46", "47", "48", "49", "51", "52", "53", "54", "55", "56", "57", "58", "60", "61",
     "62", "63", "64", "65", "66", "81", "82", "84", "86", "90", "91", "92", "93", "94", "95",
     "98", "211", "212", "213", "216", "218", "290", "291", "297", "298", "299", "350", "351",
     "352", "353", "354", "355", "356", "357", "358", "359", "370", "371", "372", "373", "374",
     "375", "376", "377", "378", "379", "380", "381", "382", "383", "385", "386", "387", "389",
     "420", "421", "423", "500", "501", "502", "503", "504", "505", "506", "507", "508", "509",
     "590", "591", "592", "593", "594", "595", "596", "597", "598", "599", "670", "672", "673",
     "674", "675", "676", "677", "678", "679", "680", "681", "682", "683", "685", "686", "687",
     "688", "689", "690", "691", "692", "850", "852", "853", "855", "856", "880", "886", "960",
     "961", "962", "963", "964", "965", "966", "967", "968", "970", "971", "972", "973", "974",
     "975", "976", "977", "992", "993", "994", "995", "996", "998"}
    | {str(n) for n in range(220, 270)})

_BY_DIAL: dict[str, tuple[str, tuple[str, str, tuple[int, ...]]]] = {}
for _code, _meta in COUNTRIES.items():
    _BY_DIAL.setdefault(_meta[0], (_code, _meta))

_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_HYPHENS_RE = re.compile("[\u2010-\u2015\u2212]")
_NUMBER_CHARS_RE = re.compile(r"\+?[\d\s().\-/]+")
MIN_DIGITS, MAX_DIGITS = 8, 15


@dataclass
class Number:
    e164: str = ""
    problem: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.e164)


def digits_of(text: str) -> str:
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", str(text or "")))


def dial_code_of(digits: str) -> str:
    """The longest country calling code these digits start with, or ""."""
    for size in (3, 2, 1):
        if digits[:size] in _DIAL_CODES:
            return digits[:size]
    return ""


def country_of_number(e164: str) -> str:
    """The country a number's calling code certainly names, or "". +1 is shared
    by the US, Canada and the Caribbean, so it names none."""
    dial = dial_code_of(digits_of(e164))
    if not dial or dial == "1":
        return ""
    found = _BY_DIAL.get(dial)
    return found[0] if found else ""


def _nanp_ok(national: str) -> bool:
    """North America: ten digits, and neither the area code nor the exchange
    starts with 0 or 1."""
    return len(national) == 10 and national[0] in "23456789" and national[3] in "23456789"


def _international(digits: str) -> Number:
    """Digits written after a +: their calling code has to be a real one."""
    dial = dial_code_of(digits)
    if not dial:
        return Number(problem="it starts with + but no country calling code matches")
    national = digits[len(dial):]
    if dial == "1":
        if not _nanp_ok(national):
            return Number(problem="it doesn't fit a North American number")
    else:
        found = _BY_DIAL.get(dial)
        if found:
            _, (_, trunk, lengths) = found
            # "+44 (0)7700 900123": the trunk prefix written after the dial code.
            if trunk and national.startswith(trunk) and len(national) not in lengths \
                    and len(national) - len(trunk) in lengths:
                national = national[len(trunk):]
            if len(national) not in lengths:
                return Number(problem=f"it has the wrong number of digits for +{dial}")
    full = dial + national
    if not MIN_DIGITS <= len(full) <= MAX_DIGITS:
        return Number(problem="it has too few or too many digits")
    return Number(e164="+" + full)


def _national(digits: str, code: str) -> Number:
    """Digits with no +, in a known country. Either the national form or the
    dial code without its +. When both readings fit, it isn't guessed."""
    if digits.startswith("00"):
        return Number(problem="it starts with 00; write it with a + so the country is certain")
    meta = COUNTRIES.get(code)
    if not meta:
        return Number(problem=f"it has no + and Harvey doesn't know how numbers are written "
                              f"in {code}; write it with a +")
    dial, trunk, lengths = meta
    keeps_zero = code in _KEEPS_LEADING_ZERO
    local = digits[len(trunk):] if trunk and digits.startswith(trunk) else digits
    as_local = len(local) in lengths and (keeps_zero or not local.startswith("0"))
    if dial == "1":
        as_local = as_local and _nanp_ok(local)
    after_dial = digits[len(dial):] if digits.startswith(dial) else ""
    as_dial = bool(after_dial) and len(after_dial) in lengths and (
        _nanp_ok(after_dial) if dial == "1" else (keeps_zero or not after_dial.startswith("0")))
    if as_local and as_dial and local != after_dial:
        return Number(problem=f"it could be read two ways in {code}; write it with a +")
    if as_local:
        return Number(e164=f"+{dial}{local}")
    if as_dial:
        return Number(e164=f"+{dial}{after_dial}")
    return Number(problem=f"it doesn't fit a number in {code}")


def normalize_phone(raw, country: str = "") -> Number:
    """``raw`` as E.164, or the problem with it. ``country`` is a two-letter
    code; without a + and without one, the country code can't be established."""
    text = _INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", str(raw or "")))
    text = _HYPHENS_RE.sub("-", text).strip()
    if text.lower().startswith("tel:"):
        text = text[4:].strip()
    if not text:
        return Number(problem="there is no number")
    if not _NUMBER_CHARS_RE.fullmatch(text):
        return Number(problem="it has characters that aren't part of a phone number")
    digits = digits_of(text)
    if not digits:
        return Number(problem="there is no number")
    if text.startswith("+"):
        return _international(digits)
    code = str(country or "").strip().upper()
    if not code:
        return Number(problem="it has no + and no country, so the country code can't be established")
    return _national(digits, code)

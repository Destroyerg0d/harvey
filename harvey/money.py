"""Finding money figures in text, and checking them against a price list.

A follow-up email may name no money at all, and a written estimate may name only
what channels.email.inbound.price_list allows (harvey/inbound.py). Both rest on
``find_figures``. Prompts are a floor, so this is deterministic and it fails
closed: a figure it can't read as an amount (say, one spelled out) never matches
anything, and a false alarm only costs a fallback to an ordinary email.

The same amount in any common format reads the same: $1,200, $1200, USD 1,200,
1,200 USD, 1.2k, $1.2k, 1.200,00 EUR and 1,20,000 are all understood. A figure with
no currency ("1.2k") matches a listed amount in any currency; "EUR 1,200" doesn't
match "$1,200".

No harvey imports here: harvey.config validates the price list with it.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_SYMBOLS = "€£₹¥₩₽₺฿₫₪₦"
_SYMBOL_CODES = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY", "₩": "KRW", "₽": "RUB",
                 "₺": "TRY", "฿": "THB", "₫": "VND", "₪": "ILS", "₦": "NGN"}
_DOLLAR_PREFIXES = {"us": "USD", "u.s.": "USD", "au": "AUD", "a": "AUD", "ca": "CAD", "c": "CAD",
                    "nz": "NZD", "hk": "HKD", "s": "SGD", "r": "BRL"}
_WORD_CODES = {"dollar": "USD", "dollars": "USD", "bucks": "USD", "euro": "EUR", "euros": "EUR",
               "pound": "GBP", "pounds": "GBP", "rupee": "INR", "rupees": "INR", "rs": "INR",
               "rs.": "INR", "dirham": "AED", "dirhams": "AED"}
_CODES = "usd|eur|gbp|inr|aud|cad|sgd|aed|chf|jpy|nzd|zar|sek|nok|dkk|hkd|brl|mxn|sar|cny|pkr"
_NOT_LETTER_BEFORE = r"(?<![^\W\d_])"
_NOT_LETTER_AFTER = r"(?![^\W\d_])"

_NUMBER = r"\d{1,3}(?: \d{3})+(?:[.,]\d{1,2})?|\d[\d.,']*\d|\d"
_SCALE = (r"(?:k|m|mn|bn|grand|thousand|million|billion|lakhs?|lacs?|crores?|cr|tsd\.?)"
          + _NOT_LETTER_AFTER)
_PREFIX = (_NOT_LETTER_BEFORE + r"(?:us|u\.s\.|au|a|ca|c|nz|hk|s|r)\$|\$|[" + _SYMBOLS + "]|"
           + _NOT_LETTER_BEFORE + rf"(?:{_CODES}|rs\.?)" + _NOT_LETTER_AFTER)
_SUFFIX = (r"\$|[" + _SYMBOLS + "]|/-|" + _NOT_LETTER_BEFORE
           + rf"(?:{_CODES}|rs|dollars?|bucks|euros?|pounds?|rupees?|dirhams?)" + _NOT_LETTER_AFTER)
_NOT_INSIDE_A_NUMBER = r"(?<![\w.,$" + _SYMBOLS + "])"
# What a number counts when it isn't money: "1.5k products", "2,400 monthly visitors".
_UNIT = (r"\s+(?:(?:monthly|daily|weekly|yearly|annual|active|unique|new)\s+)?"
         r"(?:products?|skus?|items?|pages?|screens?|users?|visitors?|customers?|orders?|emails?"
         r"|subscribers?|words?|hours?|hrs?|days?|weeks?|months?|years?|sessions?|records?"
         r"|followers?|downloads?|requests?|rows?|entries|people|employees?|members?|stores?"
         r"|locations?|languages?|integrations?|variants?|views?|clicks?|calls?|reviews?"
         r"|listings?|templates?|sections?|revisions?|rounds?|steps?|sites?|apps?)"
         + _NOT_LETTER_AFTER)

_MONEY_RE = re.compile(
    rf"(?P<pre>{_PREFIX})\s?(?P<n1>{_NUMBER})(?:\s?(?P<s1>{_SCALE}))?(?:\s?(?P<post1>{_SUFFIX}))?"
    rf"|{_NOT_INSIDE_A_NUMBER}(?P<n2>{_NUMBER})\s?(?P<s2>{_SCALE})?\s?(?P<post2>{_SUFFIX})"
    rf"|{_NOT_INSIDE_A_NUMBER}(?P<n3>{_NUMBER})\s?(?P<s3>{_SCALE})(?!{_UNIT})",
    re.IGNORECASE)
_SPELLED_NUMBER = (r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen"
                   r"|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand"
                   r"|million|lakh|crore)\b")
_SPELLED_RE = re.compile(
    _SPELLED_NUMBER + r"(?:[\s-]+[^\W\d_]+){0,3}?[\s-]+"
    r"(?:dollars?|bucks|euros?|pounds?|rupees?|grand|lakhs?|lacs?|crores?)\b",
    re.IGNORECASE)
# "Seven thousand", with no currency: money on a line about price, or when strict.
_SPELLED_BARE_RE = re.compile(
    _SPELLED_NUMBER + r"(?:[\s-]+(?:hundred|thousand|million))+" + rf"(?!{_UNIT})", re.IGNORECASE)
# A line that talks about price makes a bare number on it a price: "Total: 1200".
_PRICE_WORDS_RE = re.compile(
    r"\b(?:price|prices|pricing|cost|costs|fee|fees|budget|total|quote|investment|deposit|upfront"
    r"|payment|payments|pay|rate|rates|charge|charges|plan|plans|package|packages|tier|tiers"
    r"|preis|kosten|prix|co[uû]t|precio|costo|prezzo)\b", re.IGNORECASE)
_BARE_RE = re.compile(rf"(?<![\w.,+/-])(?P<n>\d[\d.,']*\d|\d)(?!\w|\s?%)(?!{_UNIT})", re.IGNORECASE)
_SCALES = {"k": 1000, "m": 1_000_000, "mn": 1_000_000, "bn": 1_000_000_000, "grand": 1000,
           "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000, "lakh": 100_000,
           "lakhs": 100_000, "lac": 100_000, "lacs": 100_000, "crore": 10_000_000,
           "crores": 10_000_000, "cr": 10_000_000, "tsd": 1000, "tsd.": 1000}
_INVISIBLE_RE = re.compile("[­​-‏⁠﻿]")
# Bare numbers below this, on a line about price, are counts ("2 revisions").
_SMALLEST_BARE_PRICE = 100
_SMALLEST_BARE_ANYWHERE = 1000


@dataclass(frozen=True)
class Figure:
    raw: str
    amount: Decimal | None       # None: money, but not readable as an amount
    currency: str = ""           # "" when the figure doesn't say


def _clean(text: str) -> str:
    return _INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", str(text or "")))


def _to_decimal(raw: str) -> Decimal | None:
    """1,200 / 1.200 / 1 200 / 1,20,000 / 1.200,50 / 1,200.50 as a number."""
    text = raw.replace(" ", "").replace("'", "")
    separators = [c for c in text if c in ",."]
    if separators:
        last = separators[-1]
        if len(set(separators)) == 2:            # the last one is the decimal point
            whole, _, cents = text.rpartition(last)
            text = whole.replace(",", "").replace(".", "") + "." + cents
        elif len(separators) > 1 or len(text.rpartition(last)[2]) == 3:   # thousands
            text = text.replace(last, "")
        else:                                    # one separator, not three digits after it
            text = text.replace(last, ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _amount(number: str, scale: str | None) -> Decimal | None:
    value = _to_decimal(number)
    if value is None:
        return None
    return value * _SCALES[scale.lower()] if scale else value


def _currency(prefix: str | None, suffix: str | None) -> str:
    for mark in (prefix, suffix):
        mark = (mark or "").strip().lower()
        if not mark or mark == "/-":
            continue
        if mark.endswith("$") and mark != "$":
            return _DOLLAR_PREFIXES.get(mark[:-1], "USD")
        if mark in _SYMBOL_CODES:
            return _SYMBOL_CODES[mark]
        return _WORD_CODES.get(mark, mark.upper())
    return ""


def find_figures(text: str, strict: bool = False) -> list[Figure]:
    """Every money figure in ``text``: anything with a currency or a scale, and a
    bare number on a line about price. ``strict`` also takes any other bare number
    that could be a price (1,200, or 1200 and up), leaving out what a unit follows,
    years, and a long run of digits (a phone number)."""
    clean = _clean(text)
    figures: list[Figure] = []
    spans: list[tuple[int, int]] = []
    for m in _MONEY_RE.finditer(clean):
        number = m.group("n1") or m.group("n2") or m.group("n3")
        scale = m.group("s1") or m.group("s2") or m.group("s3")
        figures.append(Figure(m.group(0).strip(), _amount(number, scale),
                              _currency(m.group("pre"), m.group("post1") or m.group("post2"))))
        spans.append(m.span())
    for m in _SPELLED_RE.finditer(clean):
        figures.append(Figure(m.group(0).strip(), None))
        spans.append(m.span())
    chars = list(clean)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    for line in "".join(chars).split("\n"):
        on_price_line = bool(_PRICE_WORDS_RE.search(line))
        if not (on_price_line or strict):
            continue
        figures += [Figure(m.group(0), None) for m in _SPELLED_BARE_RE.finditer(line)]
        for m in _BARE_RE.finditer(line):
            raw = m.group("n")
            value = _to_decimal(raw)
            if value is None or value < _SMALLEST_BARE_PRICE:
                continue
            digits = re.sub(r"\D", "", raw)
            if not on_price_line:
                if value < _SMALLEST_BARE_ANYWHERE or len(digits) >= 9:
                    continue
                if digits == raw and raw[:2] in ("19", "20") and len(raw) == 4:
                    continue          # a year
            figures.append(Figure(raw, value))
    return figures


def parse_price_entry(raw) -> Figure | None:
    """One price_list entry: exactly one amount with its currency, else None."""
    if not isinstance(raw, str):
        return None
    figures = find_figures(raw)
    if len(figures) != 1 or figures[0].amount is None or not figures[0].currency:
        return None
    return figures[0]


def _allowed(figure: Figure, listed: list[Figure]) -> bool:
    if figure.amount is None:
        return False
    return any(figure.amount == entry.amount and (not figure.currency or figure.currency == entry.currency)
               for entry in listed)


def price_problem(text: str, price_list) -> str:
    """Why this estimate can't go to a reviewer on these prices, or "". Fails
    closed: an empty or unreadable list, no price at all, or any other figure."""
    listed = [parse_price_entry(entry) for entry in (price_list or [])]
    if not listed or any(entry is None for entry in listed):
        return "channels.email.inbound.price_list is empty or can't be read"
    figures = find_figures(text, strict=True)
    if not figures:
        return "it names no price"
    outside = [f.raw for f in figures if not _allowed(f, listed)]
    if outside:
        return f"it names {', '.join(outside[:3])}, not in channels.email.inbound.price_list"
    return ""

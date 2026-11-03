"""Which country a company is in, when its own site says so beyond doubt.

Used only to fill an EMPTY company location (companies.location), which is
what send_window.recipient_country reads as a sure country: Precleared's
``skip_countries`` and each recipient's send window follow from it. So this
errs towards saying nothing. Strong evidence, and nothing weaker:

- United States: a street address (or PO box) with a state code and a
  5-digit ZIP, or a +1 phone number with a US area code together with prices
  in US dollars. A .com domain, or dollar prices alone, never count.
- United Kingdom: a .uk domain (.co.uk, .org.uk...), or a full UK postcode
  with a real postcode area (LS1 4AP).
- Canada: a .ca domain together with a Canadian postal code.
- Australia: a .au domain together with an Australian state and postcode.

Two countries' evidence on one site is no answer. Import data counts only
when it names the country in a labelled field ("Country: United Kingdom").
Pure functions; profile.py fetches the pages.
"""

import html as _html
import re
from dataclasses import dataclass

import pytz

from harvey.send_window import US_STATES, country_for

NAMES = {"US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia"}

_STREET = (r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|"
           r"Place|Pl|Parkway|Pkwy|Highway|Hwy|Circle|Cir|Terrace|Ter|Trail|Trl|Square|Sq|"
           r"Plaza|Plz|Pike|Route|Rte|Broadway")
# "1200 Example Ave, Suite 4, Springfield, IL 62701" / "PO Box 12, Springfield, IL 62701",
# the city often on the next line (a <br> in the footer).
_US_ADDRESS = re.compile(
    r"(?:\b\d{1,6}[A-Za-z]?\s+(?:[A-Za-z0-9.'#-]+\s+){0,5}?(?i:" + _STREET + r")\.?"
    r"|\b(?i:P\.?\s?O\.?\s+Box)\s+\d{1,6})"
    r"\b[\s\S]{0,100}?[\s,]([A-Z]{2})\s+(\d{5})(?:-\d{4})?(?![\d-])")
_US_PHONE = re.compile(r"(?<![\w+])\+1[\s.\-]?\(?([2-9][0-8]\d)\)?[\s.\-]?[2-9]\d{2}[\s.\-]?\d{4}(?!\d)")
# NANP area codes that are not the United States: Canada, the Caribbean and
# the US territories (their own ISO countries), toll-free and non-geographic.
_NOT_US_AREA = frozenset("""
204 226 236 249 250 257 263 289 306 343 354 365 367 368 382 387 403 416 418 428 431 437 438
450 468 474 506 514 519 548 579 581 584 587 600 604 613 622 639 647 672 683 705 709 742 753
778 780 782 807 819 825 867 873 879 902 905 942
242 246 264 268 284 340 345 441 473 649 658 664 670 671 684 721 758 767 784 787 809 829 849
868 869 876 939
500 521 522 523 524 525 526 527 528 529 533 544 566 577 588 700 710 800 822 833 844 855 866
877 880 881 882 883 884 885 886 887 888 889 900
211 311 411 511 611 711 811 911
""".split())
_USD = re.compile(r"US\$\s?\d|\bUSD\b|(?<![A-Za-z])\$\s?\d")
_USD_MARKUP = re.compile(
    r"""(?i)["']?(?:priceCurrency|price:currency)["']?\s*(?::|content=)\s*["']USD["']""")
# Real UK postcode areas (the Crown Dependencies' GY, JE and IM are not UK).
_UK_AREAS = (
    "AB AL B BA BB BD BH BL BN BR BS BT CA CB CF CH CM CO CR CT CV CW DA DD DE DG DH DL DN DT DY "
    "E EC EH EN EX FK FY G GL GU HA HD HG HP HR HS HU HX IG IP IV KA KT KW KY L LA LD LE LL LN LS "
    "LU M ME MK ML N NE NG NN NP NR NW OL OX PA PE PH PL PO PR RG RH RM S SA SE SG SK SL SM SN SO "
    "SP SR SS ST SW SY TA TD TF TN TQ TR TS TW UB W WA WC WD WF WN WR WS WV YO ZE").split()
_UK_POSTCODE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(_UK_AREAS, key=len, reverse=True)) + r")"
    r"(\d[A-Z\d]?) (\d[ABD-HJLNP-UW-Z]{2})(?![A-Za-z0-9])")
_CA_POSTAL = re.compile(r"(?<![A-Za-z0-9])([ABCEGHJ-NPRSTVXY])\d[ABCEGHJ-NPRSTV-Z][ -]?\d[ABCEGHJ-NPRSTV-Z]\d(?![A-Za-z0-9])")
_CA_PROVINCE = {"A": "NL", "B": "NS", "C": "PE", "E": "NB", "G": "QC", "H": "QC", "J": "QC",
                "K": "ON", "L": "ON", "M": "ON", "N": "ON", "P": "ON", "R": "MB", "S": "SK",
                "T": "AB", "V": "BC", "Y": "YT"}
_AU_ADDRESS = re.compile(r"(?<![A-Za-z])(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\.?,?\s+(\d{4})(?!\d)")
# The first digit each state's postcodes start with.
_AU_FIRST = {"NSW": "12", "ACT": "02", "VIC": "38", "QLD": "49", "SA": "5", "WA": "6",
             "TAS": "7", "NT": "0"}
_LABELLED = re.compile(r"(?im)\bcountry\s*[:=]\s*([A-Za-z][A-Za-z .'-]{1,40}?)\s*(?:[,;|\n]|$)")

_BLOCKS = re.compile(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1\s*>|<!--.*?-->")
_BREAKS = re.compile(r"(?i)<br\s*/?>|</(?:p|div|li|address|td|tr|h[1-6]|section|footer|header)\s*>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_TEL = re.compile(r"""(?i)href\s*=\s*["']tel:([^"']+)["']""")


@dataclass(frozen=True)
class Country:
    code: str          # US, GB, CA, AU
    location: str      # what companies.location gets: "IL, United States"
    evidence: str      # plain words for the observation
    url: str = ""      # the page it was read on
    confidence: float = 0.9

    @property
    def text(self) -> str:
        return f"{self.code}: {self.evidence}"


def visible_text(html: str) -> str:
    """The page's text, one line per block, entities decoded."""
    text = _BREAKS.sub("\n", _BLOCKS.sub(" ", html or ""))
    text = _html.unescape(_TAGS.sub(" ", text))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _suffix_of(domain: str) -> str:
    host = (domain or "").strip().lower().rstrip(".").removeprefix("www.")
    return host.rpartition(".")[2]


def _evidence(domain: str, pages: list[tuple[str, str]]) -> dict[str, tuple[str, str, str]]:
    """Strong evidence found, by country: (location, evidence, url)."""
    tld = _suffix_of(domain)
    found: dict[str, tuple[str, str, str]] = {}
    us_states: list[tuple[str, str, str]] = []
    phones: list[tuple[str, str]] = []
    usd_url = ""
    uk_codes: list[tuple[str, str]] = []
    ca_codes: list[tuple[str, str]] = []
    au_codes: list[tuple[str, str, str]] = []
    for url, html in pages:
        text = visible_text(html)
        for m in _US_ADDRESS.finditer(text):
            if m.group(1) in US_STATES:
                us_states.append((m.group(1), m.group(2), url))
        tel_text = " ".join(t.strip() for t in _TEL.findall(html or ""))
        for m in _US_PHONE.finditer(text + "\n" + tel_text):
            if m.group(1) not in _NOT_US_AREA:
                phones.append((m.group(1), url))
        if not usd_url and (_USD.search(text) or _USD_MARKUP.search(html or "")):
            usd_url = url
        uk_codes += [(" ".join(m.group(1, 2)).replace(" ", "") + " " + m.group(3), url)
                     for m in _UK_POSTCODE.finditer(text)]
        ca_codes += [(m.group(1), url) for m in _CA_POSTAL.finditer(text)]
        au_codes += [(m.group(1), m.group(2), url) for m in _AU_ADDRESS.finditer(text)
                     if m.group(2)[0] in _AU_FIRST[m.group(1)]]

    if us_states:
        states = sorted({s for s, _, _ in us_states})
        state, zip_code, url = us_states[0]
        where = f"{states[0]}, United States" if len(states) == 1 else "United States"
        found["US"] = (where, f"a US street address with a state and ZIP code ({state} {zip_code})", url)
    elif phones and usd_url:
        area, url = phones[0]
        found["US"] = ("United States",
                       f"a +1 phone number with a US area code ({area}) and prices in US dollars", url)
    if tld == "uk":
        found["GB"] = ("United Kingdom", f"the site's domain ends in .uk ({domain})", pages[0][0] if pages else "")
    elif uk_codes:
        code, url = uk_codes[0]
        found["GB"] = ("United Kingdom", f"a UK postcode ({code})", url)
    if tld == "ca" and ca_codes:
        provinces = sorted({_CA_PROVINCE.get(letter, "") for letter, _ in ca_codes})
        where = f"{provinces[0]}, Canada" if len(provinces) == 1 and provinces[0] else "Canada"
        found["CA"] = (where, "a .ca domain with a Canadian postal code", ca_codes[0][1])
    if tld == "au" and au_codes:
        states = sorted({s for s, _, _ in au_codes})
        state, post, url = au_codes[0]
        where = f"{states[0]}, Australia" if len(states) == 1 else "Australia"
        found["AU"] = (where, f"a .au domain with an Australian address ({state} {post})", url)
    return found


def infer_country(domain: str, pages: list[tuple[str, str]]) -> Country | None:
    """The one country the site's own pages ([(url, html)]) prove, or None
    (nothing strong, or two countries)."""
    found = _evidence(domain, [(u, h) for u, h in pages if h])
    if len(found) != 1:
        return None
    code, (location, evidence, url) = next(iter(found.items()))
    return Country(code=code, location=location, evidence=evidence, url=url)


def country_from_import(*fields: str) -> Country | None:
    """The country import data named in a labelled field ("Country: Canada",
    "country=GB"), when it names exactly one, certainly. A code that is also
    a state or province ("CA") is not certain, so it doesn't count."""
    named: set[str] = set()
    for field in fields:
        for m in _LABELLED.finditer(str(field or "")):
            code, certain = country_for(m.group(1).strip(" .'-"))
            if certain and code:
                named.add(code)
    if len(named) != 1:
        return None
    code = named.pop()
    name = NAMES.get(code) or pytz.country_names.get(code, "")
    if not name or country_for(name) != (code, True):
        return None
    return Country(code=code, location=name, evidence="the data it was imported with names the country",
                   confidence=0.8)


__all__ = ["Country", "NAMES", "country_from_import", "infer_country", "visible_text"]

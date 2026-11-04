"""Money figures, and the price guard on a written estimate. Every amount here is
invented, and none is anyone's real price."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from harvey.config import InboundConfig
from harvey.money import find_figures, parse_price_entry, price_problem

LISTED = ["$7,300", "$900", "EUR 2,400"]


@pytest.mark.parametrize("text", [
    "$7,300", "$7300", "USD 7,300", "7,300 USD", "usd7300", "US$ 7,300", "7300$", "$7,300.00",
    "7.3k", "$7.3K", "7.3k USD", "7300 dollars", "7,300/-",
])
def test_the_same_amount_in_any_common_format_is_allowed(text):
    assert price_problem(f"The plan is {text}.", LISTED) == ""


@pytest.mark.parametrize("text", ["EUR 2,400", "€2,400", "2,400 EUR", "2.400,00 EUR", "2 400 €", "2.4k euros"])
def test_formats_are_normalised_in_other_currencies_too(text):
    assert price_problem(text, LISTED) == ""


@pytest.mark.parametrize("text", [
    "$7,301", "$8", "6.4k", "USD 1,111", "4100", "4,100", "Total: 1100", "Price: 350",
    "seven thousand dollars", "ten grand", "two lakh", "seven thousand", "Rs. 41,000", "₹41,000",
    "41,000/-",
])
def test_a_figure_outside_the_list_is_refused(text):
    assert "not in channels.email.inbound.price_list" in price_problem(f"$900 or {text}", LISTED)


def test_the_right_amount_in_the_wrong_currency_is_refused():
    assert "not in" in price_problem("€7,300", LISTED)
    assert "not in" in price_problem("$2,400", LISTED)


def test_one_bad_figure_among_good_ones_refuses_it():
    assert "$9,100" in price_problem("Starter $900. Growth $7,300. Scale $9,100.", LISTED)
    assert "$9,100" in price_problem("From $7,300-$9,100", LISTED)


@pytest.mark.parametrize("price_list", [[], None, ["not a price"]])
def test_an_empty_or_unreadable_list_refuses_everything(price_list):
    assert "empty or can't be read" in price_problem("$7,300", price_list)


def test_an_estimate_with_no_price_is_no_estimate():
    assert price_problem("We'd love to help with your shop.", LISTED) == "it names no price"


@pytest.mark.parametrize("text", [
    "$900 for 5,500 products over 6 weeks", "$900 and 1.5k monthly visitors", "$900, 50% upfront",
    "$900, done in 2026", "$900, 3 revisions and 150 hours", "$900 on 11/04/2026",
    "$900: Plan includes 20 pages", "$900 by 2027-01-15",
])
def test_counts_dates_and_percentages_are_not_money(text):
    assert price_problem(text, LISTED) == ""


def test_lookalikes_and_hidden_characters_dont_hide_a_figure():
    assert "not in" in price_problem("＄7,301", LISTED)        # a fullwidth dollar
    assert "not in" in price_problem("$7​,301", LISTED)   # a zero-width space


@pytest.mark.parametrize("text,found", [
    ("Quick question about your store", False),
    ("We can start with 5,500 products in 3 weeks, and reply in 24 hours", False),
    ("Back in 2026, you asked about a new site", False),
    ("around 1.5k visitors a day", False),
    ("It starts at $900", True),
    ("a budget of 6,400", True),
    ("3k euros", True),
    ("two lakh", True),
    ("two thousand products", False),
    ("Rs 900", True),
])
def test_an_email_names_no_money_but_its_counts_are_fine(text, found):
    assert bool(find_figures(text)) is found


def test_figures_read_their_amount_and_currency():
    [figure] = find_figures("about 1,20,000 INR")
    assert (figure.amount, figure.currency) == (Decimal(120000), "INR")
    [figure] = find_figures("A$1.5k")
    assert (figure.amount, figure.currency) == (Decimal(1500), "AUD")
    [figure] = find_figures("nine thousand dollars")
    assert figure.amount is None     # money, but never matches anything


@pytest.mark.parametrize("entry,ok", [
    ("$1,200", True), ("EUR 2,400", True), ("1,200 USD", True),
    ("1200", False), ("1.2k", False), ("free", False), ("$1 or $2", False), (1200, False),
])
def test_a_price_list_entry_is_one_amount_with_its_currency(entry, ok):
    assert (parse_price_entry(entry) is not None) is ok


def test_a_price_list_typo_stops_the_config_loading():
    assert InboundConfig(price_list=["$1,200"]).price_list == ["$1,200"]
    assert InboundConfig(price_list="$1,200").price_list == ["$1,200"]
    assert InboundConfig(price_list=None).price_list == []
    assert InboundConfig().price_list == []
    for bad in (["1200"], [1200], ["$1,200", "call us"]):
        with pytest.raises(ValidationError, match="price_list"):
            InboundConfig(price_list=bad)

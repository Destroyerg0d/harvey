"""COUNTRY: where a company is, only when its own site (or the data it was
imported with) proves it, and an empty location filled from that, which is
the sure country Precleared's skip_countries and the send window read.
Fictional shops on mocked HTTP; 555-01xx numbers and reserved domains."""

import os
import tempfile

import httpx
import pytest
import pytest_asyncio

import harvey.collectors.profile as profile_mod
from harvey.collectors.country import country_from_import, infer_country
from harvey.collectors.profile import ProfileCollector
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.send_window import recipient_country
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

US_FOOTER = ("<footer><p>Example Hardware</p><p>1200 Example Ave, Suite 4<br>"
             "Springfield, IL 62701</p></footer>")
# A contact page long enough to count as one (profile._first_present).
CONTACT = "<p>" + "Opening hours, directions and parking. " * 20 + "</p>" + US_FOOTER


def page(body: str) -> str:
    return f"<html><head><title>Example Shop</title></head><body>{body}</body></html>"


def infer(domain: str, *bodies: str):
    return infer_country(domain, [(f"https://{domain}/{i}", page(b)) for i, b in enumerate(bodies)])


# ── What counts ──

@pytest.mark.parametrize("domain, body, code, location", [
    ("example.com", US_FOOTER, "US", "IL, United States"),
    ("example.com", "<p>PO Box 77, Springfield, IL 62701-1234</p>", "US", "IL, United States"),
    ("example.com", "<p>Call +1 (217) 555-0100</p><p>Gift card $25.00</p>", "US", "United States"),
    ("example.com", '<a href="tel:+12175550100">Call us</a><p>Prices in USD</p>', "US", "United States"),
    ("example.com", "<p>12 Example Street, Leeds LS1 4AP</p>", "GB", "United Kingdom"),
    ("example.co.uk", "<p>Free delivery on orders over 50 pounds</p>", "GB", "United Kingdom"),
    ("example.org.uk", "<p>About us</p>", "GB", "United Kingdom"),
    ("example.ca", "<p>100 Example Rd, Toronto ON M5V 2T6</p>", "CA", "ON, Canada"),
    ("example.com.au", "<p>1 Example St, Sydney NSW 2000</p>", "AU", "NSW, Australia"),
])
def test_strong_evidence_names_the_country(domain, body, code, location):
    found = infer(domain, body)
    assert found is not None and (found.code, found.location) == (code, location)
    assert found.text.startswith(code + ": ")


@pytest.mark.parametrize("domain, body, why", [
    ("example.com", "<p>Free shipping on orders over $50</p>", ".com and dollar prices alone"),
    ("example.com", "<p>Prices in USD. We ship worldwide.</p>", "USD alone"),
    ("example.com", "<p>Call +1 (217) 555-0100</p>", "a US phone without US prices"),
    ("example.com", "<p>Call +1 (416) 555-0100</p><p>$25.00</p>", "a Canadian area code"),
    ("example.com", "<p>Call +1 (800) 555-0100</p><p>$25.00</p>", "a toll-free number"),
    ("example.com", "<p>Toronto ON M5V 2T6</p>", "a Canadian postcode without .ca"),
    ("example.ca", "<p>Shop online</p>", ".ca without a postal code"),
    ("example.com", "<p>1 Example St, Sydney NSW 2000</p>", "an Australian address without .au"),
    ("example.com.au", "<p>1 Example St, Sydney NSW 7000</p>", "a postcode outside its state"),
    ("example.com", "<p>Order code AB12 3CD-X</p><p>12 Example Street, Leeds JE2 4AA</p>",
     "no real UK postcode area"),
    ("example.co.uk", US_FOOTER, "two countries"),
    ("example.com", "<p>Model IL 62701 in stock</p>", "a state and ZIP with no street"),
    ("example.com", "<script>var a='1200 Example Ave, Springfield, IL 62701'</script>",
     "text a visitor never sees"),
])
def test_anything_weaker_says_nothing(domain, body, why):
    assert infer(domain, body) is None, why


def test_the_contact_page_counts_with_the_homepage():
    assert infer("example.com", "<p>Welcome</p>", US_FOOTER).code == "US"


def test_addresses_in_two_states_are_the_country_alone():
    two = US_FOOTER + "<p>88 Example Blvd, Austin, TX 73301</p>"
    assert infer("example.com", two).location == "United States"


@pytest.mark.parametrize("fields, code", [
    (("Imported from a trade list. Country: United Kingdom",), "GB"),
    (("source=list; country=US",), "US"),
    (("", "Country: Canada"), "CA"),
])
def test_import_data_that_names_the_country(fields, code):
    assert country_from_import(*fields).code == code


@pytest.mark.parametrize("fields", [
    ("Country: CA",),                         # California or Canada
    ("We ship to every country: fast",),
    ("Country: United Kingdom", "Country: Canada"),
    ("Based in the United States",),          # not a labelled field
    ("",),
])
def test_import_data_that_doesnt_settle_it(fields):
    assert country_from_import(*fields) is None


# ── Through a profile pass ──

@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "country.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


def mock_site(monkeypatch, pages: dict[str, str]):
    """Every host answers with ``pages`` by path ("/" the homepage), else 404."""
    asked = []

    def handler(request):
        asked.append(request.url.path or "/")
        body = pages.get(request.url.path or "/")
        return httpx.Response(200, html=page(body)) if body is not None else httpx.Response(404)

    def client(*, timeout=15.0, headers=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout,
                                 headers=headers, follow_redirects=True)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(profile_mod, "public_http_client", client)
    monkeypatch.setattr(profile_mod.asyncio, "sleep", no_sleep)
    return asked


async def profile(state, domain, location="", notes="", confirmed=("COUNTRY",)):
    cid = await state.add_company(Company(name="Example Shop", domain=domain,
                                          location=location, notes=notes))
    async with ProfileCollector(state, set(confirmed)) as c:
        await c.run([{"id": cid, "name": "Example Shop", "domain": domain}])
    rows = await state.get_observations(company_id=cid, signal_code="COUNTRY", latest_only=True)
    return cid, (rows[0] if rows else None), await state.get_company(cid)


@pytest.mark.asyncio
async def test_a_proven_country_fills_an_empty_location(state, monkeypatch):
    mock_site(monkeypatch, {"/": US_FOOTER})
    _, row, company = await profile(state, "shop-one.example")
    assert company.location == "IL, United States"
    assert row["value_text"].startswith("US: a US street address")
    assert row["value_num"] == 1 and row["confidence"] >= 0.8 and row["evidence_url"]


@pytest.mark.asyncio
async def test_a_location_already_set_is_never_changed(state, monkeypatch):
    asked = mock_site(monkeypatch, {"/": "<p>Welcome</p>", "/contact": CONTACT})
    _, row, company = await profile(state, "shop-two.example", location="Leeds, United Kingdom")
    assert company.location == "Leeds, United Kingdom"
    assert row["value_text"] == ""                    # nothing on the homepage
    assert "/contact" not in asked                    # no extra fetch for a known location


@pytest.mark.asyncio
async def test_the_contact_page_is_read_when_the_homepage_says_nothing(state, monkeypatch):
    mock_site(monkeypatch, {"/": "<p>Welcome</p>", "/contact": CONTACT})
    _, row, company = await profile(state, "shop-three.example")
    assert company.location == "IL, United States" and row["value_text"].startswith("US:")


@pytest.mark.asyncio
async def test_weak_evidence_records_none_and_leaves_the_location_empty(state, monkeypatch):
    mock_site(monkeypatch, {"/": "<p>Free shipping over $50 to anywhere</p>"})
    _, row, company = await profile(state, "shop-four.example")
    assert company.location == ""
    assert row is not None and row["value_text"] == "" and row["value_num"] == 0


@pytest.mark.asyncio
async def test_import_data_naming_the_country_fills_it(state, monkeypatch):
    mock_site(monkeypatch, {"/": "<p>Welcome</p>"})
    _, row, company = await profile(state, "shop-five.example", notes="Country: United Kingdom")
    assert company.location == "United Kingdom" and row["value_text"].startswith("GB:")


@pytest.mark.asyncio
async def test_nothing_is_collected_until_country_is_confirmed(state, monkeypatch):
    mock_site(monkeypatch, {"/": US_FOOTER})
    _, row, company = await profile(state, "shop-six.example", confirmed=("TECH_STACK",))
    assert row is None and company.location == ""


@pytest.mark.asyncio
async def test_companies_profiled_before_come_back_once_when_confirmed(state, monkeypatch):
    mock_site(monkeypatch, {"/": "<p>Welcome</p>"})
    cid = await state.add_company(Company(name="Example Shop", domain="shop-seven.example"))
    async with ProfileCollector(state, {"TECH_STACK", "NO_SCHEMA_MARKUP"}) as c:
        await c.run([{"id": cid, "name": "Example Shop", "domain": "shop-seven.example"}])
    assert await state.companies_needing_profile() == []
    await state.set_signal_status("COUNTRY", "confirmed")
    assert [c["id"] for c in await state.companies_needing_profile()] == [cid]
    async with ProfileCollector(state, {"COUNTRY"}) as c:
        await c.run([{"id": cid, "name": "Example Shop", "domain": "shop-seven.example"}])
    assert await state.companies_needing_profile() == []          # checked: no answer is an answer


# ── The sure country Precleared reads ──

@pytest.mark.asyncio
async def test_the_filled_location_is_the_sure_country_precleared_skips(state, monkeypatch):
    from harvey.integrations.precleared import PreclearedClient
    mock_site(monkeypatch, {"/": US_FOOTER})
    cid, _, _ = await profile(state, "shop-eight.example")
    prospect = await state.get_prospect(await state.add_prospect(Prospect(
        company_id=cid, first_name="Jo", last_name="Example", company="Example Shop",
        email="jo@shop-eight.example", email_status="verified")))
    assert await recipient_country(state, prospect) == "US"
    check = PreclearedClient(sender_country="GB", skip_countries=["US"],
                             transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    cleared = await check.clear(prospect.email, state, country="US")
    assert cleared.allowed and cleared.skipped


@pytest.mark.asyncio
async def test_a_weak_guess_never_becomes_a_sure_country(state, monkeypatch):
    mock_site(monkeypatch, {"/": "<p>All prices in USD. Free shipping over $50.</p>"})
    cid, _, _ = await profile(state, "shop-nine.example")
    prospect = await state.get_prospect(await state.add_prospect(Prospect(
        company_id=cid, first_name="Jo", last_name="Example", company="Example Shop",
        email="jo@shop-nine.example", email_status="verified")))
    assert await recipient_country(state, prospect) == ""     # so Precleared still checks

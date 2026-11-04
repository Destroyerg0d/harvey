"""Who may be messaged on WhatsApp: the channel's core rule."""

import inspect

import pytest

from harvey.models.company import Company
from harvey.models.conversation import Conversation
from harvey.models.prospect import Prospect
from harvey.whatsapp import cli as wa_cli, contacts, queue_contact
from harvey.whatsapp.contacts import fix_number, stop_reason
from tests.whatsapp_fakes import JANE, SAM, World, make_config, run


@pytest.fixture
def w(tmp_path):
    return World(tmp_path, linked=False)


def add(w, phone=JANE, source="website contact form", **kw):
    kw.setdefault("config", w.config)
    return run(queue_contact(w.state, phone=phone, source=source, **kw))


def prospect(w, **kw):
    kw.setdefault("email", "jane@example.org")
    return run(w.state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="Founder", **kw)))


# ── Source, and one person at a time ──

def test_a_source_is_required(w):
    with pytest.raises(TypeError):
        run(queue_contact(w.state, phone=JANE, config=w.config))
    for blank in ("", "   ", None):
        with pytest.raises(ValueError, match="source"):
            add(w, source=blank)
    assert run(w.state.wa_contacts()) == []


def test_the_signature_is_keyword_only_with_source_required():
    params = inspect.signature(queue_contact).parameters
    assert all(p.kind == p.KEYWORD_ONLY for name, p in params.items() if name != "state")
    assert params["source"].default is inspect.Parameter.empty


@pytest.mark.parametrize("source", ["scraped from their site", "bought list", "lead list from a vendor",
                                    "bulk upload", "contacts.csv", "CSV import"])
def test_a_source_that_says_it_wasnt_theirs_to_give_is_refused(w, source):
    result = add(w, source=source)
    assert result.status == "refused" and run(w.state.wa_contacts()) == []


def test_there_is_no_way_to_import_a_list():
    assert not [name for name in vars(wa_cli) if "import" in name.lower()]
    assert not [name for name in vars(contacts) if "import" in name.lower() or "bulk" in name.lower()]


def test_a_person_goes_in_queued_with_their_details(w):
    result = add(w, name="Jane Doe", email="JANE@example.org", context="Asked about a rebuild",
                 source="website chat")
    assert result.queued
    contact = run(w.state.wa_get_contact(result.contact_id))
    assert contact["phone"] == JANE and contact["stage"] == "queued"
    assert contact["email"] == "jane@example.org" and contact["source"] == "website chat"
    assert contact["context"] == "Asked about a rebuild" and contact["country"] == "GB"


def test_a_country_can_be_a_name_or_a_code(w):
    assert add(w, phone="07700 900124", country="United Kingdom").queued
    assert add(w, phone="07700 900125", country="uk").queued
    assert add(w, phone="07700 900126", country="GB").queued


# ── Never guess ──

def test_no_country_code_means_needs_review_and_no_number(w):
    result = add(w, phone="07700 900123", name="Jane Doe")
    assert result.status == "needs_review" and "can't be established" in result.reason
    contact = run(w.state.wa_get_contact(result.contact_id))
    assert contact["stage"] == "needs_review" and contact["phone"] == ""
    assert contact["raw_phone"] == "07700 900123" and contact["review_reason"]
    # Asking again changes nothing, and nothing is drafted for them.
    assert add(w, phone="07700 900123").status == "duplicate"
    assert len(run(w.state.wa_contacts())) == 1
    run(w.state.set_setting("whatsapp:linked_at", "2026-01-01T00:00:00"))
    w.go()
    assert w.driver.visited[1:] == [] and w.brain.calls == []


def test_the_owner_fixes_a_number_by_giving_it_properly(w):
    result = add(w, phone="07700 900123")
    ok, why = run(fix_number(w.state, w.config, result.contact_id, "07700 900123"))
    assert not ok and "can't be established" in why
    ok, _ = run(fix_number(w.state, w.config, result.contact_id, "+44 7700 900123"))
    assert ok
    contact = run(w.state.wa_get_contact(result.contact_id))
    assert contact["stage"] == "queued" and contact["phone"] == JANE and contact["review_reason"] == ""
    ok, why = run(fix_number(w.state, w.config, result.contact_id, "+44 7700 900124"))
    assert not ok and "isn't waiting" in why


def test_a_fixed_number_is_still_checked(w):
    w.config.channels.whatsapp.do_not_contact = [JANE]
    result = add(w, phone="07700 900123")
    ok, why = run(fix_number(w.state, w.config, result.contact_id, JANE))
    assert not ok and "do_not_contact" in why
    assert run(w.state.wa_get_contact(result.contact_id))["stage"] == "needs_review"


# ── Dedupe ──

def test_the_same_number_written_differently_is_one_contact(w):
    first = add(w, phone="+44 7700 900123", name="Jane Doe")
    again = add(w, phone="07700 900123", country="GB", email="jane@example.org", name="Someone Else")
    assert first.queued and again.status == "duplicate" and again.contact_id == first.contact_id
    contact = run(w.state.wa_get_contact(first.contact_id))
    assert contact["email"] == "jane@example.org"      # a blank is filled
    assert contact["name"] == "Jane Doe"               # what it has stays
    assert len(run(w.state.wa_contacts())) == 1


# ── Refused: any channel's no, the lists, a live email conversation ──

def test_opted_out_by_email_is_refused(w):
    prospect(w, status="opted_out")
    result = add(w, email="jane@example.org")
    assert result.status == "refused" and "opted out" in result.reason
    assert run(w.state.wa_contacts()) == []


def test_said_no_by_email_is_refused(w):
    prospect(w, status="lost")
    assert "said no" in add(w, email="jane@example.org").reason


def test_said_no_on_linkedin_is_refused(w):
    pid = prospect(w)
    run(w.state.li_upsert_lead("jane-doe-1", stage="declined", name="Jane Doe", prospect_id=pid))
    assert "LinkedIn" in add(w, email="jane@example.org").reason


def test_said_no_on_whatsapp_is_final(w):
    first = add(w)
    run(w.state.wa_set_stage(first.contact_id, "declined"))
    again = add(w, source="left it again on the site")
    assert again.status == "refused" and "said no" in again.reason
    assert w.stage(first.contact_id) == "declined"


def test_the_do_not_contact_list(tmp_path):
    w = World(tmp_path, linked=False, do_not_contact=["+44 7700 900123"])
    assert "do_not_contact" in add(w).reason
    w.config.compliance.do_not_contact = ["blocked.example", "jane@example.org"]
    assert "do-not-contact" in add(w, phone="+447700900124", email="jane@example.org").reason
    assert "do-not-contact" in add(w, phone="+447700900125", email="anyone@blocked.example").reason


def test_a_company_on_the_do_not_contact_list(w):
    cid = run(w.state.add_company(Company(name="Example Goods", domain="goods.example")))
    prospect(w, company_id=cid)
    w.config.compliance.do_not_contact = ["goods.example"]
    assert "company" in add(w, email="jane@example.org").reason


def test_a_number_on_the_list_added_while_harvey_runs_holds_at_once(tmp_path):
    w = World(tmp_path, linked=False)
    path = tmp_path / "harvey.yaml"
    path.write_text("channels:\n  whatsapp:\n    do_not_contact: ['+44 7700 900123']\n", encoding="utf-8")
    object.__setattr__(w.config, "_source_path", str(path))
    assert "do_not_contact" in add(w).reason


def test_a_mistake_in_the_number_list_is_refused_when_loaded():
    with pytest.raises(Exception, match="isn't a number"):
        make_config(do_not_contact=["07700 900123"])


def test_a_live_email_conversation_is_refused(w):
    prospect(w, status="replied")
    result = add(w, email="jane@example.org")
    assert result.status == "refused" and "live email" in result.reason


def test_an_open_conversation_is_refused_even_before_their_status_changes(w):
    pid = prospect(w, status="contacted")
    run(w.state.add_conversation(Conversation(id="c1", prospect_id=pid, status="open")))
    assert "live email" in add(w, email="jane@example.org").reason


def test_someone_at_their_company_answered(w):
    cid = run(w.state.add_company(Company(name="Example Goods", domain="goods.example")))
    other = run(w.state.add_prospect(Prospect(first_name="Ada", last_name="Lane", title="Owner",
                                              email="ada@goods.example", company_id=cid)))
    prospect(w, email="jane@goods.example", company_id=cid)
    run(w.state.stop_company(other))
    assert "company answered" in add(w, email="jane@goods.example").reason


def test_someone_merely_emailed_cold_can_still_be_queued_and_is_linked(w):
    pid = prospect(w, status="contacted")
    result = add(w, email="jane@example.org")
    assert result.queued
    assert run(w.state.wa_get_contact(result.contact_id))["prospect_id"] == pid


def test_a_stored_number_links_them_however_it_was_written(w):
    pid = prospect(w, status="opted_out", phone="07700 900123")
    assert "opted out" in add(w).reason
    assert pid


def test_their_reply_doesnt_stop_the_answer_but_a_no_does(w):
    pid = prospect(w, status="replied")
    contact = {"phone": JANE, "email": "jane@example.org", "prospect_id": pid}
    assert run(stop_reason(w.state, w.config, contact, replying=True)) == ""
    assert "live email" in run(stop_reason(w.state, w.config, contact))
    run(w.state.update_prospect_status(pid, "opted_out"))
    assert "opted out" in run(stop_reason(w.state, w.config, contact, replying=True))


def test_a_refusal_is_logged_with_no_number(w):
    prospect(w, status="opted_out")
    add(w, email="jane@example.org")
    rows = run(w.state._wa_query("SELECT details_json FROM actions WHERE action_type = 'whatsapp_refused'"))
    assert rows and "7700" not in rows[0]["details_json"]


def test_no_config_loads_it_and_a_broken_one_refuses(w, monkeypatch):
    def boom():
        raise RuntimeError("no such file")
    monkeypatch.setattr("harvey.config.load_config", boom)
    result = run(queue_contact(w.state, phone=SAM, source="website chat"))
    assert result.status == "refused" and "do-not-contact" in result.reason

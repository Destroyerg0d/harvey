"""More than one sending mailbox: config, the Mailbox list, token paths and
providers. With no mailboxes configured, everything must be exactly the
single mailbox of persona.email."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import harvey.integrations.gmail as gmail
import harvey.integrations.mail_provider as mail_provider
import harvey.mailboxes as mb
import harvey.state as state_mod
from harvey.config import HarveyConfig, load_config
from harvey.paths import PROJECT_ROOT

PRIMARY = "primary@sender-one.test"
SECOND = "second@sender-one.test"
THIRD = "third@sender-two.test"

BASE = {
    "persona": {"name": "Alex Example", "company": "Example Co", "role": "Owner",
                "email": PRIMARY, "linkedin": "", "tone": "plain"},
    "product": {"name": "P", "description": "D", "pricing": "$1", "key_benefits": [],
                "objection_responses": {}},
    "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
}
MAILBOXES = [
    {"email": PRIMARY, "name": "Alex"},
    {"email": "  Second@Sender-One.TEST ", "name": " Sam Sample ", "daily_limit": 12},
    {"email": THIRD, "name": "Robin", "daily_limit": 0},
]


def make_config(mailboxes=None, persona_email=PRIMARY, **email) -> HarveyConfig:
    data = json.loads(json.dumps(BASE))
    data["persona"]["email"] = persona_email
    data["channels"] = {"email": {"provider": "gmail", "max_daily_sends": 30, **email}}
    if mailboxes is not None:
        data["channels"]["email"]["mailboxes"] = mailboxes
    return HarveyConfig(**data)


def test_the_tests_run_from_the_worktree_they_test():
    assert Path(mb.__file__).resolve().is_relative_to(PROJECT_ROOT)
    assert Path(state_mod.__file__).resolve().is_relative_to(PROJECT_ROOT)


# ── Config ──

def test_no_mailboxes_is_the_single_persona_mailbox():
    config = make_config()
    assert config.channels.email.mailboxes == []
    assert not mb.is_multi(config)
    [only] = mb.configured_mailboxes(config)
    assert only == mb.Mailbox(email=PRIMARY, name="Alex Example", daily_limit=30,
                              token_path=gmail.TOKEN_FILE, is_primary=True)
    assert mb.primary_mailbox(config) == only
    assert only.first_name == "Alex" and only.domain == "sender-one.test"


def test_a_blank_mailboxes_key_is_none_configured():
    assert make_config(mailboxes=None).channels.email.mailboxes == []
    data = json.loads(json.dumps(BASE))
    data["channels"] = {"email": {"provider": "gmail", "mailboxes": None}}
    assert not mb.is_multi(HarveyConfig(**data))


def test_mailboxes_are_read_in_order_with_their_limits():
    config = make_config(MAILBOXES)
    boxes = mb.configured_mailboxes(config)
    assert [m.email for m in boxes] == [PRIMARY, SECOND, THIRD]
    assert [m.daily_limit for m in boxes] == [30, 12, 30]   # 0: max_daily_sends
    assert [m.is_primary for m in boxes] == [True, False, False]
    assert boxes[1].name == "Sam Sample" and boxes[1].first_name == "Sam"
    assert boxes[2].domain == "sender-two.test"
    assert boxes[0].token_path == gmail.TOKEN_FILE
    assert boxes[1].token_path == gmail.TOKENS_DIR / f"{SECOND}.json"
    assert gmail.TOKENS_DIR == PROJECT_ROOT / "data" / "gmail_tokens"


def test_the_primary_need_not_be_listed_first():
    config = make_config([{"email": SECOND, "name": "Sam"}, {"email": PRIMARY, "name": "Alex"}],
                         persona_email="Primary@Sender-One.test")
    assert mb.primary_mailbox(config).email == PRIMARY
    assert [m.is_primary for m in mb.configured_mailboxes(config)] == [False, True]


@pytest.mark.parametrize("mailboxes,email,expect", [
    ([{"email": PRIMARY, "name": "A"}, {"email": PRIMARY.upper(), "name": "B"}], {},
     "twice"),
    ([{"email": PRIMARY, "name": "A"}], {"provider": "smtp"}, "provider: gmail only"),
    ([{"email": SECOND, "name": "Sam"}], {}, "must be one of channels.email.mailboxes"),
    ([{"email": "not-an-address", "name": "A"}], {}, "is not an email address"),
    ([{"email": "a/b@sender-one.test", "name": "A"}], {}, "is not an email address"),
    ([{"email": PRIMARY, "name": "  "}], {}, "needs a name"),
    ([{"email": PRIMARY, "name": "A", "daily_limit": -1}], {}, "daily_limit must be >= 0"),
])
def test_bad_mailboxes_are_refused_with_a_reason(mailboxes, email, expect):
    with pytest.raises(ValidationError) as e:
        make_config(mailboxes, **email)
    assert expect in str(e.value)


def test_load_config_reads_mailboxes_from_yaml(tmp_path):
    import yaml
    data = json.loads(json.dumps(BASE))
    data["channels"] = {"email": {"provider": "gmail", "max_daily_sends": 25,
                                  "mailboxes": MAILBOXES}}
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = load_config(str(path))
    assert [m.email for m in mb.configured_mailboxes(config)] == [PRIMARY, SECOND, THIRD]


# ── Lookups ──

def test_mailbox_for_and_normalize():
    config = make_config(MAILBOXES)
    assert mb.mailbox_for(config, "").email == PRIMARY
    assert mb.mailbox_for(config, None).email == PRIMARY
    assert mb.mailbox_for(config, " SECOND@sender-one.test ").email == SECOND
    assert mb.mailbox_for(config, "stranger@sender-one.test") is None
    assert mb.normalize(config, "") == PRIMARY
    assert mb.normalize(config, "Third@Sender-Two.test") == THIRD


def test_single_mode_lookups_are_the_persona():
    config = make_config()
    assert mb.mailbox_for(config, "").email == PRIMARY
    assert mb.mailbox_for(config, PRIMARY.upper()).email == PRIMARY
    assert mb.mailbox_for(config, SECOND) is None
    assert mb.normalize(config, "") == PRIMARY


def test_settings_keys_are_per_lower_cased_address():
    assert mb.paused_key(" Second@Sender-One.test") == "sending_paused:" + SECOND
    assert mb.bounce_key(SECOND) == "bounce_count:" + SECOND
    assert mb.read_ok_key(SECOND) == "mailbox_read_ok:" + SECOND
    assert mb.down_since_key(SECOND) == "mailbox_down_since:" + SECOND
    # Never the global keys themselves.
    assert mb.PAUSED_KEY_PREFIX != "sending_paused" and mb.BOUNCE_KEY_PREFIX != "bounce_count"


# ── Token paths ──

def test_token_path_for_primary_and_others():
    assert mb.token_path_for(PRIMARY, PRIMARY) == gmail.TOKEN_FILE
    assert mb.token_path_for(PRIMARY.upper(), " " + PRIMARY) == gmail.TOKEN_FILE
    assert mb.token_path_for(SECOND, PRIMARY) == (
        PROJECT_ROOT / "data" / "gmail_tokens" / f"{SECOND}.json")
    # Single mode with no persona.email: the one token file, as before.
    assert mb.token_path_for("", "") == gmail.TOKEN_FILE


def test_token_path_reads_the_token_file_location_when_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(gmail, "TOKEN_FILE", tmp_path / "moved.json")
    monkeypatch.setattr(gmail, "TOKENS_DIR", tmp_path / "boxes")
    assert mb.token_path_for(PRIMARY, PRIMARY) == tmp_path / "moved.json"
    assert mb.token_path_for(SECOND, PRIMARY) == tmp_path / "boxes" / f"{SECOND}.json"
    assert mb.primary_mailbox(make_config(MAILBOXES)).token_path == tmp_path / "moved.json"


@pytest.mark.parametrize("bad", ["../x@sender-one.test", "a\\b@sender-one.test", "c:x@y.test", ""])
def test_a_token_path_never_leaves_the_token_folder(bad):
    with pytest.raises(ValueError):
        mb.token_path_for(bad, PRIMARY)


def test_is_connected_reads_the_token_file_only(tmp_path):
    def box(path):
        return mb.Mailbox(email=SECOND, name="Sam", daily_limit=1, token_path=path,
                          is_primary=False)
    missing_dir = tmp_path / "never-made" / f"{SECOND}.json"
    assert not mb.is_connected(box(missing_dir))
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
    assert mb.is_connected(box(path))
    path.write_text(json.dumps({"scope": "x"}), encoding="utf-8")
    assert not mb.is_connected(box(path))
    path.write_text("not json", encoding="utf-8")
    assert not mb.is_connected(box(path))
    path.write_text("[1]", encoding="utf-8")
    assert not mb.is_connected(box(path))


# ── Providers ──

def test_single_mode_provider_is_get_mail_provider_with_two_arguments(monkeypatch):
    sentinel = object()
    calls = []
    monkeypatch.setattr(mail_provider, "get_mail_provider",
                        lambda c, e: calls.append((c, e)) or sentinel)
    config, env = make_config(), SimpleNamespace()
    assert mb.providers_for(config, env) == {PRIMARY: sentinel}
    assert calls == [(config, env)]


def test_single_mode_instantly_has_no_providers():
    assert mb.providers_for(make_config(provider="instantly"), SimpleNamespace()) == {}


def test_multi_mode_builds_one_gmail_provider_per_mailbox():
    config = make_config(MAILBOXES)
    providers = mb.providers_for(config, SimpleNamespace(gmail_client_id="i",
                                                         gmail_client_secret="s"))
    assert list(providers) == [PRIMARY, SECOND, THIRD]
    for address, provider in providers.items():
        assert isinstance(provider, gmail.GmailProvider)
        assert provider.mailbox.email == address
    assert providers[SECOND].mailbox.token_path == gmail.TOKENS_DIR / f"{SECOND}.json"


def test_get_mail_provider_passes_a_mailbox_only_when_given():
    config = make_config(MAILBOXES)
    second = mb.mailbox_for(config, SECOND)
    assert mail_provider.get_mail_provider(config, SimpleNamespace()).mailbox is None
    assert mail_provider.get_mail_provider(config, SimpleNamespace(), mailbox=second).mailbox == second
    with pytest.raises(ValueError):
        mail_provider.get_mail_provider(make_config(provider="smtp"), SimpleNamespace(),
                                        mailbox=second)

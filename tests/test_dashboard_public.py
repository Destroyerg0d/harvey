"""The dashboard at a public address (owner, 2026-11-18: an employee uses it
from elsewhere, through a tunnel with a sign-in in front). The server still
listens on loopback alone; only the one configured HTTPS name is added to the
Host and Origin checks, and the token is still required. Names are fictional."""

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import dashboard_auth as auth
from tests.test_dashboard_auth import ORIGIN, env  # noqa: F401  (fixture)

PUBLIC = "https://harvey.example.com"


@pytest.fixture
def public(env):
    dash.public_address.configure(PUBLIC)
    yield env
    dash.public_address.configure("")


def client(base=PUBLIC, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(dash.app, base_url=base, headers=headers)


@pytest.mark.parametrize("url, host", [
    ("https://harvey.example.com", "harvey.example.com"),
    ("https://Harvey.Example.com/", "harvey.example.com"),
    ("  https://team.harvey.example.co.uk ", "team.harvey.example.co.uk"),
])
def test_a_public_address_is_an_https_name_and_nothing_else(url, host):
    assert auth.public_host_of(url) == host


@pytest.mark.parametrize("url", [
    "http://harvey.example.com", "https://harvey.example.com:8443", "https://harvey.example.com/app",
    "https://harvey.example.com/?x=1", "https://localhost", "https://127.0.0.1", "https://[::1]",
    "https://10.0.0.5", "https://user@harvey.example.com", "harvey.example.com", "https://harvey",
    "https://-bad.example.com", "",
])
def test_anything_else_is_not_a_public_address(url):
    assert auth.public_host_of(url) == ""
    address = auth.PublicAddress()
    if url:
        with pytest.raises(ValueError):
            address.configure(url)
    assert address.host == "" and not address.matches("harvey.example.com")


def test_the_public_name_works_with_the_token(public):
    with client(token=public["token"]) as c:
        assert c.get("/api/companies").status_code == 200
        browser = {"Origin": PUBLIC, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
        assert c.get("/api/companies", headers=browser).status_code == 200
        assert c.post("/api/sending/pause", headers=browser).status_code == 200


def test_the_public_name_still_needs_the_token(public):
    with client() as c:
        assert c.get("/").status_code == 200                       # the shell, no data
        r = c.get("/api/companies")
        assert r.status_code == 401 and public["token"] not in r.text


@pytest.mark.parametrize("headers", [
    {"Origin": "http://harvey.example.com"},                     # not over https
    {"Origin": "https://evil.example"},
    {"Origin": "https://harvey.example.com.evil.example"},
    {"Origin": ORIGIN},                                           # the loopback page, on the public name
    {"Sec-Fetch-Site": "cross-site"},
])
def test_other_origins_on_the_public_name_are_refused(public, headers):
    with client(token=public["token"]) as c:
        assert c.get("/api/companies", headers=headers).status_code == 403
        assert c.post("/api/sending/pause", headers=headers).status_code == 403


@pytest.mark.parametrize("host", ["harvey.example.com:443", "harvey.example.com:5555",
                                  "other.example.com", "evil.harvey.example.com", "harvey.example.com."])
def test_only_the_exact_public_name_is_accepted(public, host):
    with client(token=public["token"]) as c:
        assert c.get("/api/companies", headers={"Host": host}).status_code == 400


def test_loopback_keeps_working_with_a_public_name_set(public):
    with client(base=ORIGIN, token=public["token"]) as c:
        assert c.get("/api/companies").status_code == 200
        # The public page's origin isn't this page's.
        assert c.get("/api/companies", headers={"Origin": PUBLIC}).status_code == 403


def test_without_a_public_address_the_name_is_refused(env):
    dash.public_address.configure("")
    with client(token=env["token"]) as c:
        assert c.get("/api/companies").status_code == 400


def test_the_public_unlock_link(public, monkeypatch):
    assert dash.dashboard_url(public=True) == f"{PUBLIC}/#token={public['token']}"
    dash.public_address.configure("")
    monkeypatch.setattr(dash, "configure_public_address", lambda: "")
    with pytest.raises(dash.DashboardSetupError):
        dash.dashboard_url(public=True)


def test_the_setting_is_read_from_the_config_file(env, monkeypatch):
    from types import SimpleNamespace as NS
    import harvey.config as config
    monkeypatch.setattr(config, "load_config", lambda *a, **k: NS(dashboard=NS(public_url=PUBLIC)))
    try:
        assert dash.configure_public_address() == PUBLIC
        monkeypatch.setattr(config, "load_config",
                            lambda *a, **k: NS(dashboard=NS(public_url="http://harvey.example.com")))
        assert dash.configure_public_address() == "" and dash.public_address.host == ""
    finally:
        dash.public_address.configure("")

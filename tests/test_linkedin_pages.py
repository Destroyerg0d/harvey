"""LinkedIn page parsing, on hand-written fixtures in LinkedIn's markup shape."""

from harvey.linkedin import pages
from tests.linkedin_fakes import thread_html

INBOX = """
<ul class="msg-conversations-container__conversations-list">
  <li class="msg-conversation-listitem msg-conversation-card__convo-item--unread">
    <a href="/messaging/thread/2-abc123/?trk=x"><h3> Jane   Doe </h3>
    <p class="msg-conversation-card__message-snippet">Sounds good, what would it cost?</p>
    <time>2:14 PM</time></a></li>
  <li class="msg-conversation-listitem">
    <a href="/messaging/thread/2-def456/"><h3>Omar Field</h3>
    <p class="msg-conversation-card__message-snippet">You: Thanks Omar, talk soon.</p>
    <time>Oct 30</time></a></li>
  <li class="msg-conversation-listitem"><p>no name here</p></li>
</ul>"""


def test_conversation_list_reads_name_snippet_and_who_sent_last():
    cards = pages.parse_conversation_list(INBOX)
    assert [c.name for c in cards] == ["Jane Doe", "Omar Field"]
    jane, omar = cards
    assert not jane.we_sent_last and jane.snippet == "Sounds good, what would it cost?"
    assert jane.unread and jane.time == "2:14 PM"
    assert jane.thread_url == "https://www.linkedin.com/messaging/thread/2-abc123/"
    assert omar.we_sent_last and omar.snippet == "Thanks Omar, talk soon."


def test_thread_gives_each_body_the_last_named_sender():
    html = thread_html("Jane Doe", [("Sam Owner", "Hi Jane, saw your shop."),
                                    ("Jane Doe", "Thanks!"), ("Jane Doe", "What would it cost?")])
    thread = pages.parse_thread(html)
    assert thread.header == "Jane Doe"
    assert [(m.sender, m.body) for m in thread.messages] == [
        ("Sam Owner", "Hi Jane, saw your shop."), ("Jane Doe", "Thanks!"),
        ("Jane Doe", "What would it cost?")]
    assert thread.last().body == "What would it cost?"


def test_compose_link_skips_other_peoples_message_buttons_and_keeps_two_params():
    html = """
    <aside><a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AOTHER&recipient=OTHER"
              aria-label="Message Riley Other">Message</a></aside>
    <main><a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AJD1&recipient=JD1&screenContext=X&trk=y"
             aria-label="Message Jane Doe">Message</a></main>"""
    link = pages.compose_link_from_profile(html, "Jane Doe")
    assert link == ("https://www.linkedin.com/messaging/compose/"
                    "?profileUrn=urn%3Ali%3Afsd_profile%3AJD1&recipient=JD1")
    assert pages.compose_link_from_profile(html, "Someone Else") == ""


def test_compose_link_accepts_an_unlabelled_button():
    html = '<a href="/messaging/compose/?recipient=JD1">Message</a>'
    assert pages.compose_link_from_profile(html, "Jane Doe").endswith("?recipient=JD1")


def test_recipient_check_reads_pills_and_the_header():
    html = thread_html("New message", [], recipient_pill="Jane Doe")
    assert pages.recipient_confirmed(html, "Jane Doe")
    assert not pages.recipient_confirmed(html, "Omar Field")
    assert pages.recipient_confirmed(thread_html("Jane Doe, MBA", []), "jane doe")


def test_names_match_ignores_case_accents_and_suffixes():
    assert pages.names_match("José Álvarez", "jose alvarez")
    assert pages.names_match("Jane Q. Doe", "Jane Doe")
    assert pages.names_match("Jane Doe (she/her)", "Jane Doe")
    assert not pages.names_match("Jane Doe", "Jane Smith")
    assert not pages.names_match("", "Jane Doe")


def test_connections_page_gives_slug_name_and_date():
    html = """<main><ul>
      <li><a href="/in/jane-doe-1a2b/"><span>Jane Doe</span></a><p>Founder</p>
          <time>Connected on October 28, 2026</time></li>
      <li><a href="https://www.linkedin.com/in/omar-field/"><span>Omar Field</span></a>
          <time>Connected on Nov 2, 2026</time></li>
      <li><a href="/in/no-date/">No Date</a></li>
    </ul></main>"""
    conns = pages.parse_connections(html)
    assert [(c.slug, c.name, str(c.connected_on)) for c in conns] == [
        ("jane-doe-1a2b", "Jane Doe", "2026-10-28"), ("omar-field", "Omar Field", "2026-11-02")]


def test_sent_invitations_with_their_age():
    html = """<ul>
      <li><a href="/in/ada-lane/">Ada Lane</a><span>Sent 3 weeks ago</span></li>
      <li><a href="/in/bo-chen/">Bo Chen</a><span>Sent today</span></li>
      <li><a href="/in/cy-park/">Cy Park</a><span>Sent a month ago</span></li>
    </ul>"""
    invites = {i.slug: i.age_days for i in pages.parse_sent_invitations(html)}
    assert invites == {"ada-lane": 21, "bo-chen": 0, "cy-park": 30}


def test_profile_connect_states():
    card = '<main><section class="artdeco-card pv-top-card">{}</section></main>'
    assert pages.profile_connect_state(card.format(
        '<button aria-label="Invite Jane Doe to connect">Connect</button>')) == "connect"
    assert pages.profile_connect_state(card.format('<button>Pending</button>')) == "pending"
    assert pages.profile_connect_state(card.format(
        '<span>1st</span><a aria-label="Message Jane Doe" href="/messaging/compose/">Message</a>')) == "connected"
    assert pages.profile_connect_state(card.format('<button>More</button>')) == "more"
    assert pages.profile_connect_state("<main></main>") == "unknown"


def test_note_limit_and_invite_confirmation():
    assert pages.note_limit_reached(
        "<div role='dialog'>You’ve used all your free personalized invitations this month.</div>")
    assert not pages.note_limit_reached("<div role='dialog'><button>Add a note</button></div>")
    assert pages.invite_confirmed("<div class='artdeco-toast-item'>Invitation sent</div>")
    assert not pages.invite_confirmed("<main><button>Connect</button></main>")


def test_slugs_and_signed_in():
    assert pages.slug_from_url("https://www.linkedin.com/in/Jane-Doe-1a2b/?x=1") == "jane-doe-1a2b"
    assert pages.slug_from_url("https://www.linkedin.com/company/acme/") == ""
    nav = '<header id="global-nav"></header>'
    assert pages.signed_in_from("https://www.linkedin.com/feed/", nav)
    assert not pages.signed_in_from("https://www.linkedin.com/login", nav)
    assert not pages.signed_in_from("https://www.linkedin.com/feed/", "<form class='login__form'></form>")
    # A profile whose slug starts with "login" is still a profile.
    assert pages.signed_in_from("https://www.linkedin.com/in/loginov/", nav)

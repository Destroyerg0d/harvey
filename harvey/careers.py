"""People who write about a job, and people selling to us.

Owner, 2026-11-14: "make sure not to talk with people who are applying for
career, do not engage with them, just tell them to contact HR mail or LinkedIn
of the company", and on every channel, "make sure not to engage with people
who you think are not potential clients".

So, on email, WhatsApp and LinkedIn alike:

- A job seeker (a job, an internship, freelance work with us, a CV) gets ONE
  fixed reply pointing to HR (``careers.email``) and the company's LinkedIn
  page (``careers.linkedin_url``), and then nothing more from Harvey. With
  neither set they get no reply.
- Someone selling to us (their services, backlinks, leads, a "partnership"
  that is a pitch) gets no reply at all.

The reply is our own fixed text, not a model's, so it goes out without
waiting for approval: it is marked ``approved_by = CAREERS_APPROVED`` and the
senders let exactly that text through (``is_careers_reply``).
"""

from __future__ import annotations

JOB_SEEKER = "job_seeker"
VENDOR = "vendor"
NOT_A_CLIENT = frozenset({JOB_SEEKER, VENDOR})
CAREERS_APPROVED = "careers"

# For the classifier prompts (email, WhatsApp and LinkedIn share one).
CLASSIFIER_LINES = (
    '- "job_seeker" — wants a job, an internship or freelance work with us, or sends a CV or '
    "portfolio to be hired\n"
    '- "vendor" — is selling something to us (their services, SEO, backlinks, leads, software, '
    "outsourcing, a partnership that is really a pitch), not looking to buy from us"
)

STOPPED_JOB = "they asked about a job: pointed to HR"
STOPPED_VENDOR = "they are selling to us, not buying"


def _careers(config):
    return getattr(config, "careers", None)


def careers_contacts(config) -> tuple[str, str]:
    c = _careers(config)
    return ((getattr(c, "email", "") or "").strip(),
            (getattr(c, "linkedin_url", "") or "").strip())


def _company(config) -> str:
    persona = getattr(config, "persona", None)
    return (getattr(persona, "company", "") or "").strip() or "us"


def careers_core(config) -> str:
    """The reply without a greeting, or "" when no careers contact is set."""
    email, linkedin = careers_contacts(config)
    company = _company(config)
    if email and linkedin:
        where = (f"please email our HR team at {email} or message {company} on LinkedIn "
                 f"({linkedin})")
    elif email:
        where = f"please email our HR team at {email}"
    elif linkedin:
        where = f"please message {company} on LinkedIn ({linkedin})"
    else:
        return ""
    return (f"Thanks for getting in touch. I don't handle hiring, so for jobs and internships "
            f"{where}. The HR team answers much faster there.")


def careers_reply(config, first_name: str = "") -> str:
    core = careers_core(config)
    if not core:
        return ""
    name = (first_name or "").strip().split(" ")[0]
    return f"Hi {name},\n\n{core}" if name else core


def is_careers_reply(body: str, config) -> bool:
    """Exactly the fixed reply (with or without its greeting): the one text
    that goes out marked CAREERS_APPROVED without a person approving it."""
    core = careers_core(config)
    text = (body or "").strip()
    if not core or not text.endswith(core):
        return False
    greeting = text[:-len(core)].strip()
    return greeting == "" or (greeting.startswith("Hi ") and greeting.endswith(",")
                              and len(greeting) <= 40 and "/" not in greeting
                              and "@" not in greeting and "." not in greeting)

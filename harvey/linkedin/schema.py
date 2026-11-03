"""The LinkedIn tables. Created by StateManager.init_db on every start with
CREATE TABLE IF NOT EXISTS (no numbered migration, so upstream's numbering
is untouched). No imports: harvey.state reads this at import time."""

# Where a lead is. Forward moves only (store.advance_stage); declined is final.
STAGES = ("found", "invited", "accepted", "messaged", "replied", "warm", "meeting",
          "declined", "withdrawn")
MESSAGE_KINDS = ("invite_note", "message", "reply")
MESSAGE_STATUSES = ("draft", "approved", "sent", "failed", "skipped")

LINKEDIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS linkedin_leads (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    headline TEXT DEFAULT '',
    company TEXT DEFAULT '',
    profile_url TEXT DEFAULT '',
    slug TEXT NOT NULL UNIQUE,
    urn TEXT DEFAULT '',
    prospect_id TEXT DEFAULT '',
    company_id TEXT DEFAULT '',
    stage TEXT DEFAULT 'found',
    invited_at TIMESTAMP,
    accepted_at TIMESTAMP,
    last_outbound_at TIMESTAMP,
    last_inbound_at TIMESTAMP,
    next_action_at TIMESTAMP,
    note TEXT DEFAULT '',
    source TEXT DEFAULT '',
    -- Set when this person must never get a message (a colleague of
    -- another lead, say): the reason, shown on the dashboard.
    no_message_reason TEXT DEFAULT '',
    thread_url TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_linkedin_leads_stage ON linkedin_leads(stage);
CREATE INDEX IF NOT EXISTS idx_linkedin_leads_prospect ON linkedin_leads(prospect_id);

CREATE TABLE IF NOT EXISTS linkedin_messages (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    direction TEXT DEFAULT 'out',
    kind TEXT DEFAULT 'message',
    body TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',
    error TEXT DEFAULT '',
    thread_url TEXT DEFAULT '',
    -- An inbound message's intent once Harvey has read it ('' = not yet).
    intent TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP,
    approved_by TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_linkedin_messages_lead ON linkedin_messages(lead_id, created_at);
CREATE INDEX IF NOT EXISTS idx_linkedin_messages_status ON linkedin_messages(status);
"""

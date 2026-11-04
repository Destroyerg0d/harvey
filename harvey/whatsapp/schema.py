"""The WhatsApp tables. Created by StateManager.init_db on every start with
CREATE TABLE IF NOT EXISTS (no numbered migration, like LinkedIn's). No
imports: harvey.state reads this at import time."""

# Where a contact is. Forward moves only (store.advance_stage); declined is
# final, no_whatsapp holds until the number is changed, needs_review until the
# owner has fixed the number.
STAGES = ("queued", "messaged", "replied", "warm", "meeting", "declined", "no_whatsapp",
          "needs_review")
MESSAGE_KINDS = ("first", "follow_up", "reply")
# maybe_sent: Enter was pressed and the bubble couldn't be confirmed. Held for
# the owner and never retried: a double message is worse than a missed one.
MESSAGE_STATUSES = ("draft", "approved", "sent", "failed", "skipped", "maybe_sent")

WHATSAPP_SCHEMA = """
CREATE TABLE IF NOT EXISTS whatsapp_contacts (
    id TEXT PRIMARY KEY,
    -- E.164, or '' while the number needs review (raw_phone is what we were given).
    phone TEXT DEFAULT '',
    raw_phone TEXT DEFAULT '',
    name TEXT DEFAULT '',
    email TEXT DEFAULT '',
    prospect_id TEXT DEFAULT '',
    country TEXT DEFAULT '',
    -- Where they gave us the number. Required: nobody gets here without one.
    source TEXT NOT NULL DEFAULT '',
    context TEXT DEFAULT '',
    stage TEXT DEFAULT 'queued',
    review_reason TEXT DEFAULT '',
    -- Set while another channel stops this contact: the reason, for the dashboard.
    no_message_reason TEXT DEFAULT '',
    last_outbound_at TIMESTAMP,
    last_inbound_at TIMESTAMP,
    next_action_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_contacts_phone
    ON whatsapp_contacts(phone) WHERE phone != '';
CREATE INDEX IF NOT EXISTS idx_whatsapp_contacts_stage ON whatsapp_contacts(stage);
CREATE INDEX IF NOT EXISTS idx_whatsapp_contacts_prospect ON whatsapp_contacts(prospect_id);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    direction TEXT DEFAULT 'out',
    kind TEXT DEFAULT 'first',
    body TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',
    error TEXT DEFAULT '',
    -- An inbound message's intent once Harvey has read it ('' = not yet).
    intent TEXT DEFAULT '',
    -- WhatsApp's id for the bubble, when the page shows one.
    wa_id TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP,
    approved_by TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_contact ON whatsapp_messages(contact_id, created_at);
CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_status ON whatsapp_messages(status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_messages_wa_id
    ON whatsapp_messages(wa_id) WHERE wa_id != '';
"""

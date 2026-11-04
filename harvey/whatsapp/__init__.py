"""Harvey on WhatsApp, through WhatsApp Web: first messages, one follow-up, replies.

Automating WhatsApp breaks its terms and can get the number banned, so this
runs on a separate number the owner can afford to lose, off by default, at
deliberately low limits, and it stops hard on any warning.

Only people who gave us their number themselves are ever messaged
(``queue_contact``): a source is required, there is no bulk import, and a
number whose country code can't be established is held for the owner rather
than guessed. Nothing goes out until it is approved, unless WhatsApp autopilot
is on.

Modules:
    numbers   E.164, and the rule that a number is never guessed
    schema    the two tables, created by StateManager.init_db
    store     state methods for them (mixed into StateManager)
    contacts  queue_contact, and who may not be messaged (all channels)
    pages     every selector, and pure parsing of WhatsApp Web's page
    guards    ban, warning, signed-out and offline detection, and the pauses
    pacing    caps, the 14-day ramp, delays and recipients' hours
    session   the Chrome profile, and linking the phone by QR code
    web       the dashboard routes
    cli       `harvey whatsapp ...`
"""

from harvey.whatsapp.contacts import QueueResult, queue_contact

__all__ = ["QueueResult", "queue_contact"]

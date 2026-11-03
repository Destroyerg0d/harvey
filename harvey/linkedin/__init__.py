"""Harvey on LinkedIn: invites, messages, follow-ups and replies.

One dedicated, visible Chrome profile the owner signs in to personally
(Harvey never handles the password or a 2FA code), human pacing, daily and
weekly caps, and a hard stop on any warning LinkedIn shows. Drafts wait for
approval on the dashboard's LinkedIn tab unless LinkedIn autopilot is on.

Modules:
    schema    the two tables, created by StateManager.init_db
    store     state methods for them (mixed into StateManager)
    pages     pure parsing of LinkedIn page HTML
    guards    warning / captcha / limit detection and the 48-hour pause
    pacing    LinkedIn hours, delays and caps
    session   the Chrome profile and the page driver
    importer  the tracker import (pipeline JSON + sends workbook)
    web       the dashboard routes
    cli       `harvey linkedin ...`
"""

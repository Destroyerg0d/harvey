"""`harvey whatsapp login|status|pause|resume|run|add|approve|skip`.

`add` takes one person at a time, on purpose: there is no way to import a list.
"""

import asyncio
import json


def add_parser(subparsers):
    sub = subparsers.add_parser(
        "whatsapp", help="WhatsApp: link the phone, add someone who gave you their number, run, pause")
    sub.add_argument("whatsapp_action",
                     choices=["login", "status", "pause", "resume", "run", "add", "approve", "skip"],
                     help="login: link the phone by QR code yourself; add PHONE --source S: queue one "
                          "person who gave you their number; approve ID (or --all) / skip ID: a draft; "
                          "pause/resume: the kill switch; run: one session now")
    sub.add_argument("target", nargs="?", default="", metavar="PHONE_OR_ID",
                     help="add: the number, with a + or with --country; approve/skip: the message id")
    sub.add_argument("--source", default="", help="add: where they gave you this number (required)")
    sub.add_argument("--name", default="", help="add: their name")
    sub.add_argument("--email", default="", help="add: their email")
    sub.add_argument("--context", default="", help="add: what they told you (all a first message may use)")
    sub.add_argument("--country", default="", help="add: their country, when the number has no +")
    sub.add_argument("--all", action="store_true", dest="approve_all",
                     help="approve: every draft except those held for a look")
    sub.add_argument("--once", action="store_true", help="run: one session now, even if one ran recently")
    sub.add_argument("--dry-run", action="store_true", help="run: read WhatsApp and draft, but send nothing")
    sub.add_argument("--reason", default="", help="pause: why (shown on the dashboard)")
    sub.set_defaults(func=cmd_whatsapp)
    return sub


def cmd_whatsapp(args):
    from harvey.config import load_config
    from harvey.state import StateManager

    config = load_config()
    state = StateManager()
    asyncio.run(_ACTIONS[args.whatsapp_action](args, config, state))


async def _login(args, config, state):
    """The link itself, whether you ran this or the dashboard's Connect did: it
    writes how it went where the dashboard reads it (harvey/channel_login.py)."""
    from harvey import channel_login
    from harvey.agents.whatsapp_agent import channel_on
    from harvey.whatsapp import guards
    from harvey.whatsapp.pacing import mark_linked
    from harvey.whatsapp.session import login_interactive
    await state.init_db()
    cfg = config.channels.whatsapp
    if await channel_login.session_going(state, "whatsapp", cfg):
        print("\n  A WhatsApp session is running in that Chrome profile. Try again when it has finished.\n")
        return
    why: list[str] = []
    numbers: list[str] = []
    linked = ""

    async def attempt():
        ok = await login_interactive(cfg, stopped=why.append, number_seen=numbers.append)
        return ok, "; ".join(why)

    async def connected():
        nonlocal linked
        await guards.clear_pause(state, kinds=("signed_out",))
        linked = await mark_linked(state, cfg, number=numbers[-1] if numbers else "")

    outcome = await channel_login.run_attended(state, "whatsapp", attempt,
                                               minutes=cfg.login_timeout_minutes, on_connected=connected)
    if outcome.connected:
        print(f"\n  Linked. The profile keeps the link in {cfg.profile_dir}. Linked since {linked[:16]} UTC; "
              f"the first {cfg.new_number_days} days run at {cfg.max_daily_first_messages_new} first "
              "messages a day.\n")
        if not await channel_on(state, config):
            print("  WhatsApp is still off: turn it on on the dashboard's WhatsApp tab to let Harvey run it.\n")
    elif outcome.status == channel_login.TIMED_OUT:
        print("\n  Not linked yet. Run `harvey whatsapp login` again when you have the phone to hand.\n")
    elif outcome.status == channel_login.CANCELLED:
        print("\n  Cancelled from the dashboard. Nothing changed.\n")
    else:
        print(f"\n  {outcome.detail}\n")


async def _add(args, config, state):
    from harvey.whatsapp.contacts import queue_contact
    await state.init_db()
    try:
        result = await queue_contact(state, phone=args.target, name=args.name, email=args.email,
                                     context=args.context, source=args.source, country=args.country,
                                     config=config)
    except ValueError as e:
        print(f"\n  {str(e).capitalize()}. Example: harvey whatsapp add +14155550100 "
              "--source \"website contact form\"\n")
        return
    lines = {
        "queued": "Queued. Harvey drafts their first message on its next run; it waits for your approval.",
        "needs_review": f"Held for you, not queued: {result.reason}. Fix it on the dashboard's WhatsApp tab.",
        "duplicate": "Already here: nothing changed but any blank details.",
        "refused": f"Refused: {result.reason}.",
    }
    print(f"\n  {lines[result.status]}\n")


async def _status(args, config, state):
    from harvey.agents.whatsapp_agent import LAST_RUN_KEY, autopilot_on, channel_on
    from harvey.whatsapp import guards
    from harvey.whatsapp.pacing import NO_CAP, WhatsAppPacing
    await state.init_db()
    cfg = config.channels.whatsapp
    pacing = WhatsAppPacing(state, cfg)
    pause = await guards.active_pause(state)
    used, caps = await pacing.used(), await pacing.caps()
    funnel = await state.wa_funnel()
    drafts = await state.wa_messages(statuses=("draft",), direction="out")
    approved = await state.wa_messages(statuses=("approved",), direction="out")
    maybe = await state.wa_messages(statuses=("maybe_sent",), direction="out")
    linked = await pacing.linked_at()
    try:
        last = json.loads(await state.get_setting(LAST_RUN_KEY) or "{}")
    except ValueError:
        last = {}
    print("\n  WhatsApp: " + ("on" if await channel_on(state, config) else "off")
          + (", autopilot" if await autopilot_on(state, config) else ", approval mode"))
    number = cfg.number or "(channels.whatsapp.number isn't set)"
    print(f"  Number: {number}, "
          + (f"linked {linked:%Y-%m-%d %H:%M} UTC" if linked else "not linked: run `harvey whatsapp login`"))
    print(f"  Hours: 10:00-19:00 by each recipient's clock when known, else {pacing.hours_text()}")
    if pause:
        print(f"  PAUSED: {pause.get('reason')}"
              + (f" (until {str(pause['until'])[:16]} UTC)" if pause.get("until") else ""))
    def cap(n):
        return "no cap" if n >= NO_CAP else n
    print(f"  Today: {used['first_today']}/{cap(caps['first_today'])} first messages, "
          f"{used['messages_today']}/{cap(caps['messages_today'])} in total, "
          f"{used['messages_hour']}/{cap(caps['messages_hour'])} this hour")
    print(f"  Funnel: {funnel['contacts']} contacts, {funnel['messaged']} messaged, {funnel['replied']} replied, "
          f"{funnel['positive']} positive, {funnel['meetings']} meetings, {funnel['declined']} declined, "
          f"{funnel['no_whatsapp']} not on WhatsApp, {funnel['needs_review']} numbers to check")
    print(f"  Queue: {len(drafts)} drafts waiting for you, {len(approved)} approved to send")
    if maybe:
        print(f"  {len(maybe)} may have gone out: check the chat, then answer on the dashboard's WhatsApp tab")
    for m in drafts[:20]:
        body = (m["body"] or "").replace("\n", " ")
        print(f"    {m['id']}  {m['kind']}: {body[:90]}")
    if drafts:
        print("  Approve with: harvey whatsapp approve ID   (or --all)")
    if last.get("started"):
        print(f"  Last run: {last.get('started')} UTC, {last.get('outcome') or 'running'}")
    print()


async def _approve(args, config, state):
    await state.init_db()
    if args.approve_all:
        print(f"\n  {await state.wa_approve(None)} approved. Any held for a look wait for you by id.\n")
    elif args.target:
        done = await state.wa_approve(args.target)
        print("\n  Approved.\n" if done else "\n  No draft with that id. See `harvey whatsapp status`.\n")
    else:
        print("\n  Give a message id, or --all.\n")


async def _skip(args, config, state):
    await state.init_db()
    done = await state.wa_skip(args.target) if args.target else 0
    print("\n  Skipped.\n" if done else "\n  Give the id of a waiting message. See `harvey whatsapp status`.\n")


async def _pause(args, config, state):
    from harvey.whatsapp import guards
    await state.init_db()
    await guards.set_pause(state, args.reason or "Paused by you (harvey whatsapp pause)",
                           kind="manual", hours=None)
    print("\n  WhatsApp paused. Nothing is read or sent there until `harvey whatsapp resume`.\n")


async def _resume(args, config, state):
    from harvey.whatsapp import guards
    await state.init_db()
    lifted = await guards.clear_pause(state)
    print("\n  WhatsApp resumed.\n" if lifted else "\n  WhatsApp wasn't paused.\n")


async def _run(args, config, state):
    from harvey.agents.whatsapp_agent import WhatsAppAgent
    from harvey.brain import Brain
    await state.init_db()
    agent = WhatsAppAgent(Brain(state, config.brain), state, config)
    report = await agent.run(dry_run=args.dry_run, ignore_interval=args.once)
    if not report.get("ran"):
        print(f"\n  Not run: {report.get('reason')}\n")
        return
    print(f"\n  WhatsApp run {'(dry run) ' if args.dry_run else ''}{report.get('outcome')}:")
    print(f"    {report['inbound']} new replies, {report['declined']} declined, {report['drafted']} drafts written")
    print(f"    {report['sent']} sent, {report['failed']} failed, {report['maybe_sent']} may have gone out, "
          f"{report['no_whatsapp']} not on WhatsApp")
    for item in report.get("would_send") or []:
        body = (item.get("body") or "").replace("\n", " ")
        print(f"    would send {item['kind']} to {item.get('contact')}: {body[:90]}")
    print()


_ACTIONS = {"login": _login, "add": _add, "status": _status, "approve": _approve, "skip": _skip,
            "pause": _pause, "resume": _resume, "run": _run}

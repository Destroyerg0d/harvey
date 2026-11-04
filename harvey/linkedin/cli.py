"""`harvey linkedin login|import|status|pause|resume|run`."""

import asyncio
import json
from datetime import datetime, timezone


def add_parser(subparsers):
    sub = subparsers.add_parser("linkedin", help="LinkedIn: sign in, import the tracker, run, pause")
    sub.add_argument("linkedin_action", choices=["login", "import", "status", "pause", "resume", "run"],
                     help="login: sign the dedicated Chrome profile in yourself; import: load the "
                          "tracker; status; pause/resume; run: one LinkedIn session now")
    sub.add_argument("--pipeline", default="", metavar="PATH", help="import: the pipeline JSON")
    sub.add_argument("--sends", default="", metavar="PATH",
                     help="import: the sends workbook (.xlsx, sheet \"Verified Sends\", or .csv)")
    sub.add_argument("--once", action="store_true", help="run: one session now, even if one ran "
                                                         "less than two hours ago")
    sub.add_argument("--dry-run", action="store_true",
                     help="run: read LinkedIn and draft, but send, invite and withdraw nothing")
    sub.add_argument("--reason", default="", help="pause: why (shown on the dashboard)")
    sub.set_defaults(func=cmd_linkedin)
    return sub


def cmd_linkedin(args):
    from harvey.config import load_config
    from harvey.state import StateManager

    config = load_config()
    state = StateManager()
    action = args.linkedin_action
    asyncio.run(_ACTIONS[action](args, config, state))


async def _login(args, config, state):
    from harvey.linkedin import guards
    from harvey.linkedin.session import ChromeNotFound, login_interactive
    await state.init_db()
    try:
        ok = await login_interactive(config.channels.linkedin,
                                     timeout_minutes=config.channels.linkedin.login_timeout_minutes)
    except ChromeNotFound as e:
        print(f"\n  {e}\n")
        return
    if ok:
        await guards.clear_pause(state, kinds=("signed_out",))
        await state.set_setting("linkedin:signed_in_at",
                                datetime.now(timezone.utc).isoformat(timespec="seconds"))
        print("\n  Signed in. The profile keeps the sign-in in data/linkedin-profile.\n")
        if not config.channels.linkedin.enabled:
            print("  LinkedIn is still off: set channels.linkedin.enabled: true to let Harvey run it.\n")
    else:
        print("\n  Not signed in yet (timed out). Run `harvey linkedin login` again.\n")


async def _import(args, config, state):
    from harvey.linkedin.importer import import_tracker
    if not args.pipeline and not args.sends:
        print("\n  Give --pipeline PATH and/or --sends PATH.\n")
        return
    await state.init_db()
    report = await import_tracker(state, args.pipeline or None, args.sends or None)
    print("\n  LinkedIn import:")
    for line in report.lines():
        print("   ", line)
    print()


async def _status(args, config, state):
    from harvey.agents.linkedin_agent import LAST_RUN_KEY, autopilot_on
    from harvey.linkedin import guards
    from harvey.linkedin.pacing import Pacing
    await state.init_db()
    cfg = config.channels.linkedin
    pacing = Pacing(state, cfg)
    pause = await guards.active_pause(state)
    used, caps = await pacing.used(), pacing.caps()
    funnel = await state.li_funnel()
    drafts = await state.li_messages(statuses=("draft",), direction="out")
    approved = await state.li_messages(statuses=("approved",), direction="out")
    try:
        last = json.loads(await state.get_setting(LAST_RUN_KEY) or "{}")
    except ValueError:
        last = {}
    print("\n  LinkedIn: " + ("on" if cfg.enabled else "off (channels.linkedin.enabled)")
          + (", autopilot" if await autopilot_on(state, config) else ", approval mode"))
    print(f"  Hours: {pacing.hours_text()} ({'inside' if pacing.in_hours() else 'outside'} now)")
    if pause:
        print(f"  PAUSED: {pause.get('reason')}"
              + (f" (until {str(pause['until'])[:16]} UTC)" if pause.get("until") else ""))
    print(f"  Today: {used['invites_today']}/{caps['invites_today']} invites, "
          f"{used['messages_today']}/{caps['messages_today']} messages; "
          f"this week {used['invites_week']}/{caps['invites_week']} invites")
    print(f"  Funnel: {funnel['found']} found, {funnel['invited']} invited, {funnel['accepted']} accepted, "
          f"{funnel['messaged']} messaged, {funnel['replied']} replied, {funnel['positive']} positive, "
          f"{funnel['meetings']} meetings, {funnel['declined']} declined")
    print(f"  Queue: {len(drafts)} drafts waiting for you, {len(approved)} approved to send")
    if last.get("started"):
        print(f"  Last run: {last.get('started')} UTC, {last.get('outcome') or 'running'}")
    print()


async def _pause(args, config, state):
    from harvey.linkedin import guards
    await state.init_db()
    await guards.set_pause(state, args.reason or "Paused by you (harvey linkedin pause)",
                           kind="manual", hours=None)
    print("\n  LinkedIn paused. Nothing is read or sent there until `harvey linkedin resume`.\n")


async def _resume(args, config, state):
    from harvey.linkedin import guards
    await state.init_db()
    lifted = await guards.clear_pause(state)
    print("\n  LinkedIn resumed.\n" if lifted else "\n  LinkedIn wasn't paused.\n")


async def _run(args, config, state):
    from harvey.agents.linkedin_agent import LinkedInAgent
    from harvey.brain import Brain
    await state.init_db()
    agent = LinkedInAgent(Brain(state, config.brain), state, config)
    report = await agent.run(dry_run=args.dry_run, ignore_interval=args.once)
    if not report.get("ran"):
        print(f"\n  Not run: {report.get('reason')}\n")
        return
    print(f"\n  LinkedIn run {'(dry run) ' if args.dry_run else ''}{report.get('outcome')}:")
    print(f"    {report['inbound']} new replies, {report['accepted']} accepted, "
          f"{report['withdrawn']} withdrawn, {report['declined']} declined")
    print(f"    {report['drafted']} drafts written, {report['sent_messages']} messages and "
          f"{report['sent_invites']} invites sent, {report['failed']} failed")
    for item in report.get("would_send") or []:
        body = (item.get("body") or "").replace("\n", " ")
        print(f"    would send {item['kind']} to {item.get('lead')}: {body[:90]}")
    print()


_ACTIONS = {"login": _login, "import": _import, "status": _status, "pause": _pause,
            "resume": _resume, "run": _run}

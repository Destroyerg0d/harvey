"""Read social profiles with Scout (github.com/kiryano/Scout), for Harvey.

This file runs inside Scout's own Python, not Harvey's: harvey/social_leads.py
starts it with Scout's interpreter, in Scout's folder, so Scout's scrapers and
their dependencies stay out of Harvey's environment. It uses the standard
library and Scout's scrapers only.

Input (stdin, JSON): {"platform": "instagram", "handles": ["a", "b"],
"delay": [1.5, 4.5]}. Output (stdout): one JSON line per handle,
{"handle", "ok", "profile"} or {"handle", "ok": false, "error"}.

Only the platforms in SCRAPERS can be read. LinkedIn is not among them:
Scout's LinkedIn scraper needs the owner's own session cookie, and that
account runs Harvey's LinkedIn channel. Harvey clears Scout's proxy settings
before starting this, so profiles are read directly, at Scout's pace.
"""

import importlib
import json
import os
import random
import sys
import time

# platform -> (Scout module, function taking a handle)
SCRAPERS = {
    "instagram": ("app.scrapers.instagram", "scrape_profile_no_login"),
    "tiktok": ("app.scrapers.tiktok", "scrape_tiktok_profile"),
    "youtube": ("app.scrapers.youtube", "scrape_channel"),
    "pinterest": ("app.scrapers.pinterest", "scrape_profile"),
    "linktree": ("app.scrapers.linktree", "scrape_all"),
}


def _emit(row: dict) -> None:
    sys.stdout.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        job = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        _emit({"handle": "", "ok": False, "error": f"bad input: {e}"})
        return 2
    platform = str(job.get("platform") or "").lower()
    if platform not in SCRAPERS:
        _emit({"handle": "", "ok": False, "error": f"platform not allowed: {platform or 'none'}"})
        return 2
    sys.path.insert(0, os.getcwd())                 # Scout's folder: its `app` package
    module_name, func_name = SCRAPERS[platform]
    try:
        scrape = getattr(importlib.import_module(module_name), func_name)
    except Exception as e:                           # Scout missing or changed
        _emit({"handle": "", "ok": False, "error": f"Scout's {platform} scraper didn't load: {e}"})
        return 3
    low, high = (job.get("delay") or [1.5, 4.5])[:2]
    # A run of failures means the platform is turning us away: stop there.
    stop_after = int(job.get("stop_after_failures") or 0)
    handles = [str(h).strip().lstrip("@") for h in job.get("handles") or [] if str(h).strip()]
    failures = 0
    for i, handle in enumerate(handles):
        if stop_after and failures >= stop_after:
            _emit({"handle": handle, "ok": False, "error": "not read: the platform refused the ones before"})
            continue
        if i:
            time.sleep(random.uniform(float(low), float(high)))
        try:
            profile = scrape(handle)
        except Exception as e:
            failures += 1
            _emit({"handle": handle, "ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            continue
        if not profile:
            failures += 1
            _emit({"handle": handle, "ok": False, "error": "no profile returned"})
            continue
        failures = 0
        _emit({"handle": handle, "ok": True, "profile": profile})
    return 0


if __name__ == "__main__":
    sys.exit(main())

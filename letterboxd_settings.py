"""
Your own settings — kept OUT of the code, in ~/.letterboxd_cache/settings.json,
so the project can be shared without naming you or anyone you follow.

    {
      "username": "your-letterboxd-username",
      "extra_friends": []        # accounts Letterboxd leaves out of your following pages
    }

See settings.example.json.
"""

import os
import json

PATH = os.path.expanduser("~/.letterboxd_cache/settings.json")


def load():
    try:
        with open(PATH) as f:
            return json.load(f)
    except Exception:
        return {}


_s = load()
USERNAME = _s.get("username") or os.environ.get("LETTERBOXD_USERNAME", "")
EXTRA_FRIENDS = list(_s.get("extra_friends") or [])


def require_username():
    if not USERNAME:
        raise SystemExit(f"No Letterboxd username set. Create {PATH} — see settings.example.json.")
    return USERNAME

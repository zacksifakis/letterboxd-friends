"""
Rebuild the published pages and put them online.

    python3 letterboxd_site.py              # rebuild and publish
    python3 letterboxd_site.py --no-push    # rebuild locally only

The fortnightly refresh calls this at the end, so the site follows the data
without anyone doing anything.

Publishes to whichever is set up:

  Netlify   ~/.letterboxd_cache/netlify_token   personal access token
            ~/.letterboxd_cache/netlify_site    the site's API ID
            Nothing is uploaded except the two finished pages — no repository,
            nothing public to browse.

  GitHub    a git remote on ~/Desktop/168-seats
            Note that GitHub Pages only serves public repositories on the
            free plan.

With neither configured it just rebuilds locally and says so.
"""

import io
import os
import sys
import json
import zipfile
import datetime
import subprocess
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
DESKTOP = os.path.join(HOME, "Desktop")
REPO = os.path.join(DESKTOP, "168-seats")
PAGES = [
    (os.path.join(DESKTOP, "letterboxd_publish.py"), os.path.join(REPO, "index.html"), "English"),
    (os.path.join(DESKTOP, "letterboxd_publish_el.py"), os.path.join(REPO, "el", "index.html"), "Greek"),
]


CACHE = os.path.join(HOME, ".letterboxd_cache")


def secret(name):
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read().strip()


def netlify_deploy():
    """Zip the finished pages and hand them to Netlify. Returns the live URL."""
    token, site = secret("netlify_token"), secret("netlify_site")
    if not (token and site):
        return None

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for _, target, _ in PAGES:
            if os.path.exists(target):
                z.write(target, os.path.relpath(target, REPO))
    payload = buf.getvalue()
    print(f"  uploading {len(payload) / 1e6:.1f} MB to Netlify")

    req = urllib.request.Request(
        f"https://api.netlify.com/api/v1/sites/{site}/deploys",
        data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/zip"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            info = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Netlify refused the deploy (HTTP {e.code}): {detail}")
    return info.get("ssl_url") or info.get("url") or "(deployed)"


def git(*args, check=False):
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def build():
    for script, target, label in PAGES:
        if not os.path.exists(script):
            print(f"  {label}: {script} missing, skipped")
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        r = subprocess.run([sys.executable, script, target, "--standalone"],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"building the {label} page failed: {r.stderr.strip()[-400:]}")
        print(f"  {label}: {r.stdout.strip()}")


def main():
    os.makedirs(REPO, exist_ok=True)
    print(f"Rebuilding the site in {REPO}")
    build()

    if not os.path.isdir(os.path.join(REPO, ".git")):
        git("init", "-b", "main", check=True)
        print("  started a git repository")

    # GitHub Pages serves .html as-is; this stops Jekyll from reprocessing it.
    nojekyll = os.path.join(REPO, ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w").close()

    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        print("  nothing changed since the last build")
        return

    message = f"Film counts as of {datetime.date.today():%-d %b %Y}"
    has_commit = git("rev-parse", "HEAD").returncode == 0
    git("commit", "--amend" if has_commit else "--allow-empty", "-m", message, check=True)
    print(f"  committed: {message}")

    if "--no-push" in sys.argv:
        print("  --no-push given, stopping here")
        return

    live = netlify_deploy()
    if live:
        print(f"  published: {live}")
        return

    if not git("remote", "get-url", "origin").stdout.strip():
        print("  Not published: no Netlify credentials and no git remote.")
        print("  Netlify needs ~/.letterboxd_cache/netlify_token and ~/.letterboxd_cache/netlify_site.")
        return

    # Force is intended: each build rewrites the single commit.
    push = git("push", "--force", "origin", "main")
    if push.returncode:
        print(f"  push failed: {push.stderr.strip()[-400:]}")
        sys.exit(1)
    print("  pushed to GitHub — Pages will rebuild in a minute or two")


if __name__ == "__main__":
    main()

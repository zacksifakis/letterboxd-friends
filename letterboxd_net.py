"""
How the scripts make web requests.

Plain requests with a descriptive User-Agent. If Letterboxd refuses them,
scrapes report it and leave your data untouched.

Machine-specific settings (a proxy, say) can go in an optional local module,
letterboxd_net_local.py, which isn't part of this repository.

Please respect Letterboxd's terms of use: keep request rates low and use the
results for personal, non-commercial purposes.
"""

try:                                            # a local override, never committed
    from letterboxd_net_local import session, rotate, probe, current   # noqa: F401
except ImportError:
    import requests

    UA = "letterboxd-friends (personal project)"

    def current():
        return "plain requests"

    def session(profile=None, **kw):
        s = requests.Session()
        s.headers["User-Agent"] = UA
        return s

    def rotate(note="", log=None):
        if log:
            log(f"request refused ({note})")
        return session()

    def probe(url=None, log=print):
        if url is None:
            import letterboxd_settings
            url = f"https://letterboxd.com/{letterboxd_settings.require_username()}/"
        try:
            r = session().get(url, timeout=20)
            log(f"  HTTP {r.status_code}")
            return "plain requests" if r.status_code == 200 else None
        except Exception as e:
            log(f"  {type(e).__name__}")
            return None

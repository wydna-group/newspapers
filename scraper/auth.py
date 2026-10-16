"""
Authentication helpers.

Reads a Netscape-format cookies.txt file (exported from your browser via
"Get cookies.txt LOCALLY" or similar extension) and injects those cookies
into both a requests.Session and a Playwright browser context.

Usage:
    session = build_requests_session("cookies.txt")
    async with build_playwright_context(playwright, "cookies.txt") as context:
        page = await context.new_page()
"""
import http.cookiejar
import os
from pathlib import Path

import requests

COOKIES_FILE = os.environ.get("COOKIES_FILE", "cookies.txt")

# Realistic browser User-Agent to avoid trivial bot detection
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _parse_cookies_file(cookies_file: Path) -> list[dict]:
    """
    Lenient Netscape cookies.txt parser that handles the domain_specified
    flag inconsistency produced by some browser extensions (e.g. domains
    starting with '.' but flagged FALSE).

    Returns a list of dicts with keys:
        domain, path, secure, expires, name, value
    """
    cookies = []
    with open(cookies_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _domain_specified, path, secure, expires, name, value = parts[:7]
            cookies.append({
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "expires": int(expires) if expires.isdigit() else None,
                "name": name,
                "value": value,
            })
    return cookies


def _build_cookiejar(cookies_file: Path) -> http.cookiejar.CookieJar:
    """Build a CookieJar from our lenient parser."""
    jar = http.cookiejar.CookieJar()
    for c in _parse_cookies_file(cookies_file):
        cookie = http.cookiejar.Cookie(
            version=0,
            name=c["name"],
            value=c["value"],
            port=None,
            port_specified=False,
            domain=c["domain"],
            domain_specified=bool(c["domain"]),
            domain_initial_dot=c["domain"].startswith("."),
            path=c["path"],
            path_specified=bool(c["path"]),
            secure=c["secure"],
            expires=c["expires"],
            discard=c["expires"] is None,
            comment=None,
            comment_url=None,
            rest={},
        )
        jar.set_cookie(cookie)
    return jar


def build_requests_session(cookies_file: str | Path = COOKIES_FILE) -> requests.Session:
    """
    Return a requests.Session pre-loaded with cookies from `cookies_file`
    and a browser-like User-Agent.
    """
    cookies_file = Path(cookies_file)
    if not cookies_file.exists():
        raise FileNotFoundError(
            f"Cookie file not found: {cookies_file}\n"
            "Export your newspapers.com cookies using the "
            "'Get cookies.txt LOCALLY' browser extension and save as cookies.txt"
        )

    session = requests.Session()
    session.cookies = _build_cookiejar(cookies_file)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def cookies_as_playwright_list(cookies_file: str | Path = COOKIES_FILE) -> list[dict]:
    """
    Convert cookies.txt to the list-of-dicts format Playwright expects.
    """
    cookies_file = Path(cookies_file)
    pw_cookies = []
    for c in _parse_cookies_file(cookies_file):
        pw_cookie: dict = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c["path"],
            "secure": c["secure"],
            "httpOnly": False,
        }
        if c["expires"]:
            pw_cookie["expires"] = float(c["expires"])
        pw_cookies.append(pw_cookie)
    return pw_cookies

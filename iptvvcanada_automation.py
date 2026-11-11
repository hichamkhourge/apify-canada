"""
IPTVV Canada - Automated Trial Account Creation

Automates the IPTVV.ca cart checkout flow using temporary mail.tm emails
and extracts Xtream credentials from the received email.

The browser checkout runs in a Node/Puppeteer worker (browser/checkout.js, using
puppeteer-extra + the stealth plugin); everything else is plain HTTP from Python.

Install deps: pip install -r requirements.txt && (cd browser && npm ci)
"""
import argparse
import base64
import html
import imaplib
import json
import os
import random
import re
import select
import socket
import string
import threading
import time
from datetime import datetime
import requests
from dotenv import load_dotenv
from twocaptcha import TwoCaptcha

import iboplayer_auth
import player_api
from puppeteer_client import PuppeteerCheckout, WorkerError

load_dotenv()

try:
    from telegram_notifier import notifier
except ImportError:
    class DummyNotifier:
        def notify_success(self, *args, **kwargs):
            return False

        def notify_error(self, *args, **kwargs):
            return False

    notifier = DummyNotifier()

TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY")
IPTVV_BASE_URL = os.getenv("IPTVV_BASE_URL", "https://iptvv.ca").rstrip("/")
IPTVV_CART_URL = os.getenv("IPTVV_CART_URL", f"{IPTVV_BASE_URL}/cart/")
MAILTM_API_BASE = os.getenv("MAILTM_API_BASE", "https://api.mail.tm")
EMAIL_POLL_SECONDS = int(os.getenv("IPTVV_EMAIL_POLL_SECONDS", "30"))
EMAIL_MAX_WAIT_SECONDS = int(os.getenv("IPTVV_EMAIL_MAX_WAIT_SECONDS", "2700"))  # 45 minutes

# Email backend:
#   "mailcx" (default) — api.mail.cx v1 REST API. The mailbox itself is implicit
#                (any local-part @ a system domain is live immediately, no
#                signup), but reading it needs an account API token
#                (MAILCX_API_TOKEN, from the mail.cx dashboard -> Tokens).
#                GET /v1/inbox/<address> long-polls server-side for up to 25s
#                per call, so no client-side poll loop is needed.
#   "boomlify"        — boomlify.com. By default uses the free web guest flow (no
#                key, no captcha, ~2-month inbox). If BOOMLIFY_API_KEY is set it
#                uses the paid REST API instead, with lifetime BOOMLIFY_EMAIL_TIME.
#   "tmaily"          — tmaily.com same-origin REST API. GET /generate for an
#                address (session cookie binds the inbox), GET /emails?address=
#                for messages. Inboxes auto-expire after 24h. Addresses are
#                pinned to TMAILY_DOMAIN (default imgcompress.io) when that
#                domain is still live; tmaily rotates its domain roster.
#   "gmail"           — real Gmail inbox with a unique plus-addressed alias per run
#                (e.g. wdmonitoring+55455@gmail.com), polled over IMAP with an app
#                password. Disposable-mail domains get dropped by IPTVV's mailer;
#                a real Gmail address does not.
#   "procmail"        — api.procmail.xyz REST API (powers 8gwifi.org/temp-email.jsp).
#                Pure HTTP: GET /generate for an address, GET /inbox/{addr} for messages.
#   "mailtm"          — legacy mail.tm REST API (kept as a fallback).
IPTVV_EMAIL_BACKEND = os.getenv("IPTVV_EMAIL_BACKEND", "mailcx").strip().lower()
# Backends tried in order when the primary one fails its pre-flight check or
# can't allocate an inbox (e.g. mail.cx 402 when the account is over quota).
# Comma-separated; set to "" to disable fallback and fail on the primary.
IPTVV_EMAIL_FALLBACKS = [
    b.strip().lower()
    for b in os.getenv("IPTVV_EMAIL_FALLBACKS", "gmail").split(",")
    if b.strip()
]
TMAILY_API_BASE = os.getenv("TMAILY_API_BASE", "https://tmaily.com").rstrip("/")
# Domain to mint tmaily addresses on; set to "" to let tmaily pick a random one.
TMAILY_DOMAIN = os.getenv("TMAILY_DOMAIN", "imgcompress.io").strip()
PROCMAIL_API_BASE = os.getenv("PROCMAIL_API_BASE", "https://api.procmail.xyz").rstrip("/")
MAILCX_API_BASE = os.getenv("MAILCX_API_BASE", "https://api.mail.cx/v1").rstrip("/")
MAILCX_API_TOKEN = os.getenv("MAILCX_API_TOKEN", "").strip()
BOOMLIFY_API_BASE = os.getenv("BOOMLIFY_API_BASE", "https://v1.boomlify.com").rstrip("/")
BOOMLIFY_API_KEY = os.getenv("BOOMLIFY_API_KEY", "").strip()
BOOMLIFY_EMAIL_TIME = os.getenv("BOOMLIFY_EMAIL_TIME", "1day").strip()  # 10min|1hour|1day|permanent
# Guest-mode domains to use first, in order (IPTVV rejects some Boomlify domains
# as disposable; beta.pendulum.cyou is known to pass). BOOMLIFY_DOMAIN is the
# legacy single-domain name and also the API-mode domain.
BOOMLIFY_DOMAIN = os.getenv("BOOMLIFY_DOMAIN", "").strip()
BOOMLIFY_DOMAINS = [
    d.strip().lower()
    for d in (os.getenv("BOOMLIFY_DOMAINS") or BOOMLIFY_DOMAIN or "beta.pendulum.cyou").split(",")
    if d.strip()
]
# Domains IPTVV's checkout is known to reject as temporary/disposable.
BOOMLIFY_BLOCKED_DOMAINS = {
    d.strip().lower()
    for d in os.getenv("BOOMLIFY_BLOCKED_DOMAINS", "kuromee.store").split(",")
    if d.strip()
}
# How many fresh addresses to try when checkout rejects one as disposable.
IPTVV_EMAIL_REJECT_RETRIES = int(os.getenv("IPTVV_EMAIL_REJECT_RETRIES", "4"))
# Domain to mint mailcx addresses on; leave "" to use whatever /v1/config marks default.
MAILCX_DOMAIN = os.getenv("MAILCX_DOMAIN", "").strip()
IPTVV_GMAIL_ADDRESS = os.getenv("IPTVV_GMAIL_ADDRESS", "wdmonitoring@gmail.com").strip()
IPTVV_GMAIL_APP_PASSWORD = os.getenv("IPTVV_GMAIL_APP_PASSWORD", "").strip()
IPTVV_GMAIL_IMAP_HOST = os.getenv("IPTVV_GMAIL_IMAP_HOST", "imap.gmail.com")
# Own domain with a catch-all forwarding to IPTVV_GMAIL_ADDRESS (e.g. Cloudflare
# Email Routing). When set, each run uses a fresh random address on it instead
# of a plus-alias, which IPTVV normalizes back to the base Gmail address.
IPTVV_GMAIL_ALIAS_DOMAIN = os.getenv("IPTVV_GMAIL_ALIAS_DOMAIN", "").strip().lower().lstrip("@")
AUTO_EXIT = os.getenv("AUTO_EXIT", "True").lower() == "true"
IPTVV_PAGE_LOAD_RETRIES = int(os.getenv("IPTVV_PAGE_LOAD_RETRIES", "2"))
IPTVV_TRIAL_PRODUCT_ID = os.getenv("IPTVV_TRIAL_PRODUCT_ID", "7758")
IPTVV_CLOUDFLARE_WAIT_SECONDS = int(os.getenv("IPTVV_CLOUDFLARE_WAIT_SECONDS", "45"))
IPTVV_DEBUG_DIR = os.getenv("IPTVV_DEBUG_DIR", "/app/logs")
IPTVV_PROXY_CHECK_URL = os.getenv("IPTVV_PROXY_CHECK_URL", "https://api.ipify.org")
# Optional upstream HTTP proxy for the browser, as a full URL:
#   http://user:pass@host:port  (credentials optional).
# On Apify this is populated from Actor.create_proxy_configuration().new_url()
# so browser egress uses a residential IP instead of the blocked datacenter IP.
IPTVV_PROXY_URL = os.getenv("IPTVV_PROXY_URL", "").strip()
IPTVV_KNOWN_BLOCKED_IP = os.getenv("IPTVV_KNOWN_BLOCKED_IP", "").strip()

# IBO Player integration configuration
IPTVV_IBOPLAYER_ENABLED = os.getenv("IPTVV_IBOPLAYER_ENABLED", "False").lower() == "true"
IPTVV_IBOPLAYER_COOKIE = os.getenv("IPTVV_IBOPLAYER_COOKIE", "")  # legacy; no longer used for auth
# Device-login credentials (bearer-token auth, replacing the cookie). Fall back to
# the shared IBOPLAYER_* device identity when the IPTVV-specific vars are unset.
IPTVV_IBOPLAYER_MAC_ADDRESS = os.getenv("IPTVV_IBOPLAYER_MAC_ADDRESS") or os.getenv("IBOPLAYER_MAC_ADDRESS", "")
IPTVV_IBOPLAYER_DEVICE_KEY = os.getenv("IPTVV_IBOPLAYER_DEVICE_KEY") or os.getenv("IBOPLAYER_DEVICE_KEY", "")
IPTVV_IBOPLAYER_PLAYLIST_URL_ID = os.getenv("IPTVV_IBOPLAYER_PLAYLIST_URL_ID", "")
IPTVV_IBOPLAYER_PLAYLIST_NAME = os.getenv("IPTVV_IBOPLAYER_PLAYLIST_NAME", "IPTVV Canada")
IPTVV_IBOPLAYER_PLAYLIST_URL_TEMPLATE = os.getenv("IPTVV_IBOPLAYER_PLAYLIST_URL", "http://iptvvcanada.com")

# GitHub playlist-updater integration: stores the M3U URL in a repo Actions
# secret and dispatches the workflow that republishes the playlist.
IPTVV_GITHUB_ENABLED = os.getenv("IPTVV_GITHUB_ENABLED", "False").lower() == "true"
IPTVV_GITHUB_TOKEN = (os.getenv("IPTVV_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")).strip()
IPTVV_GITHUB_REPO = os.getenv("IPTVV_GITHUB_REPO", "hichamkhourge/playlist-updater").strip()
IPTVV_GITHUB_SECRET_NAME = os.getenv("IPTVV_GITHUB_SECRET_NAME", "M3U_SOURCE_URL").strip()
IPTVV_GITHUB_WORKFLOW = os.getenv("IPTVV_GITHUB_WORKFLOW", "update-gist.yml").strip()
IPTVV_GITHUB_REF = os.getenv("IPTVV_GITHUB_REF", "main").strip()

# Email subject patterns to detect credentials email (checked case-insensitively)
CREDENTIALS_EMAIL_SUBJECTS = [
    "Free 24-Hour IPTV Trial Subscription",  # Current IPTVV format
    "Your trial is now active",               # Legacy/alternate format
    "IPTV Trial Subscription",                # Partial match
    "Your free trial login is ready",         # Format seen 2026-11
    "trial login",                            # Partial match of the above
]

# Message ids from IPTVV whose body was already fetched and found not to hold
# credentials, so the body-sniffing fallback doesn't refetch them every poll.
_non_credential_message_ids = set()
solver = TwoCaptcha(TWOCAPTCHA_API_KEY) if TWOCAPTCHA_API_KEY else None


class CloudflareBlockedError(RuntimeError):
    """Raised when IPTVV serves a Cloudflare/WAF page instead of checkout."""


class TrialRejectedError(RuntimeError):
    """Raised when IPTVV accepts checkout but refuses to issue trial credentials."""


class DisposableEmailRejectedError(TrialRejectedError):
    """Raised when checkout rejects the email address as temporary/disposable."""


# ═══════════════════════════════════════════════════════════
# Mail.tm API Helper Functions
# ═══════════════════════════════════════════════════════════

def get_available_domains():
    """Fetch list of available mail.tm domains."""
    try:
        response = requests.get(f"{MAILTM_API_BASE}/domains", timeout=10)
        response.raise_for_status()
        domains = response.json()
        if domains and "hydra:member" in domains:
            return [d["domain"] for d in domains["hydra:member"]]
        return []
    except Exception as exc:
        print(f"[!] Failed to fetch mail.tm domains: {exc}")
        return []


def create_mailtm_account():
    """
    Create a temporary email account via mail.tm API.

    Returns:
        tuple: (email_address, password, auth_token) or (None, None, None) on failure
    """
    try:
        domains = get_available_domains()
        if not domains:
            raise RuntimeError("No mail.tm domains available")

        # Generate random email
        username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        domain = random.choice(domains)
        email_address = f"{username}@{domain}"
        password = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

        print(f"[*] Creating mail.tm account: {email_address}")

        # Create account
        create_response = requests.post(
            f"{MAILTM_API_BASE}/accounts",
            json={"address": email_address, "password": password},
            timeout=10
        )
        create_response.raise_for_status()
        print(f"[OK] Mail.tm account created: {email_address}")

        # Get auth token
        token_response = requests.post(
            f"{MAILTM_API_BASE}/token",
            json={"address": email_address, "password": password},
            timeout=10
        )
        token_response.raise_for_status()
        auth_token = token_response.json().get("token")

        if not auth_token:
            raise RuntimeError("Failed to get auth token from mail.tm")

        print("[OK] Mail.tm authentication successful")
        return email_address, password, auth_token

    except Exception as exc:
        print(f"[!] Failed to create mail.tm account: {exc}")
        return None, None, None


def get_mailtm_messages(auth_token):
    """
    Fetch messages from mail.tm inbox.

    Args:
        auth_token: Bearer token for authentication

    Returns:
        list: List of message objects
    """
    try:
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(
            f"{MAILTM_API_BASE}/messages",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        return data.get("hydra:member", [])
    except Exception as exc:
        print(f"[!] Failed to fetch mail.tm messages: {exc}")
        return []


def get_mailtm_message_by_id(auth_token, message_id):
    """
    Fetch a specific message's full content from mail.tm.

    Args:
        auth_token: Bearer token for authentication
        message_id: ID of the message to retrieve

    Returns:
        dict: Message object with 'text' and 'html' fields, or None on failure
    """
    try:
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(
            f"{MAILTM_API_BASE}/messages/{message_id}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"[!] Failed to fetch message {message_id}: {exc}")
        return None


def _scan_messages_for_credentials(messages, fetch_full):
    """Scan a batch of inbox messages for the IPTVV credentials (or rejection) email.

    Args:
        messages: list of message summaries, each a dict with at least
                  'subject', 'from': {'address': ...} and 'id'.
        fetch_full: callable(msg) -> full message body dict with 'text'/'html'.
                    For mail.tm this fetches the body by id; for browser backends
                    whose messages are already complete it can return msg unchanged.

    Returns:
        The full credentials message dict, or None if not present in this batch.

    Raises:
        TrialRejectedError: if a rejection email from IPTVV is seen.
    """
    rejection_markers = [
        "already used",
        "duplicate trial",
        "trial was already used",
        "order was cancelled",
        "order was canceled",
    ]

    for msg in messages:
        subject = msg.get("subject", "")
        from_addr = msg.get("from", {}).get("address", "")

        print(f"    - From: {from_addr}, Subject: {subject}")

        lowered_subject = subject.lower()

        if "iptvv" in from_addr.lower() and any(marker in lowered_subject for marker in rejection_markers):
            full_message = fetch_full(msg)
            preview = ""
            if full_message:
                preview = full_message.get("text", "")[:500].strip()
            raise TrialRejectedError(
                f"IPTVV refused to issue trial credentials: {subject}. "
                f"Message preview: {preview}"
            )

        # Check if this is the credentials email (check multiple subject patterns)
        if any(pattern.lower() in lowered_subject for pattern in CREDENTIALS_EMAIL_SUBJECTS):
            print("[OK] Credentials email found!")
            full_message = fetch_full(msg)
            if full_message:
                return full_message
            continue

        # Fallback for unknown subjects: any IPTVV email whose body mentions
        # both a username and a password is treated as the credentials email.
        msg_key = msg.get("id") or f"{from_addr}|{subject}"
        if "iptvv" in from_addr.lower() and msg_key not in _non_credential_message_ids:
            full_message = fetch_full(msg)
            if full_message:
                body = full_message.get("text", "") or ""
                html_part = full_message.get("html", "")
                if isinstance(html_part, list):
                    html_part = " ".join(html_part)
                body = f"{body} {html_part or ''}".lower()
                if "username" in body and "password" in body:
                    print(f"[OK] Credentials email found (body match, unrecognised subject: {subject!r})")
                    return full_message
                _non_credential_message_ids.add(msg_key)

    return None


def _wait_for_credentials_email_mailtm(auth_token, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """
    Poll mail.tm inbox until credentials email arrives.

    Args:
        auth_token: Bearer token for authentication
        max_wait_seconds: Maximum time to wait (default: 2700 seconds / 45 minutes)

    Returns:
        dict: Full message object with credentials, or None if timeout
    """
    print(f"[*] Waiting for credentials email (max {max_wait_seconds}s / {max_wait_seconds//60} minutes)...")
    deadline = time.time() + max_wait_seconds
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"[*] Checking mail.tm inbox (attempt {attempt}, {remaining}s remaining)...")

        messages = get_mailtm_messages(auth_token)
        result = _scan_messages_for_credentials(
            messages, fetch_full=lambda m: get_mailtm_message_by_id(auth_token, m["id"])
        )
        if result:
            return result

        if messages:
            print(f"[*] Found {len(messages)} email(s), but credentials email not yet received")
        else:
            print("[*] Inbox is empty")

        print(f"[*] Waiting {EMAIL_POLL_SECONDS}s before next check...")
        time.sleep(EMAIL_POLL_SECONDS)

    print(f"[!] Timeout: Credentials email not received after {max_wait_seconds}s")
    return None


# ═══════════════════════════════════════════════════════════
# procmail.xyz (8gwifi.org) email backend — pure REST API
#
# 8gwifi.org/temp-email.jsp is a thin front-end over api.procmail.xyz:
#   GET /generate         -> plain-text address, e.g. "u6cvh398@goodbanners.xyz"
#   GET /inbox/{address}  -> JSON array of {Sender, Subject, ReceivedAt,
#                            PlainTextBody, HTMLBody}, or null when the inbox is
#                            empty. Bodies are returned inline.
# No browser, bot wall, or auth required, so we poll it directly with requests.
# ═══════════════════════════════════════════════════════════
def create_procmail_inbox():
    """Generate a disposable address via api.procmail.xyz.

    Returns the email address string, or None on failure.
    """
    try:
        resp = requests.get(f"{PROCMAIL_API_BASE}/generate", timeout=15)
        resp.raise_for_status()
        address = resp.text.strip().strip('"')
        if "@" not in address:
            raise RuntimeError(f"unexpected /generate response: {address[:120]!r}")
        print(f"[OK] procmail inbox created: {address}")
        return address
    except Exception as exc:
        print(f"[!] Failed to create procmail inbox: {exc}")
        return None


def _decode_mime_header(value):
    """Best-effort RFC 2047 decode of a MIME-encoded header (Subject/Sender)."""
    if not value:
        return ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _decode_qp(value):
    """Decode a quoted-printable body if it looks QP-encoded, else return as-is.

    IPTVV's mail (and procmail's PlainTextBody/HTMLBody) is quoted-printable, so
    "Username:=20ABC" / soft line breaks must be decoded before credential regexes
    can match. Plain bodies are left untouched.
    """
    if not value:
        return ""
    if not re.search(r"=[0-9A-Fa-f]{2}|=\r?\n", value):
        return value
    try:
        import quopri
        return quopri.decodestring(value.encode("utf-8", "replace")).decode("utf-8", "replace")
    except Exception:
        return value


def get_procmail_messages(address):
    """Fetch the procmail inbox and normalize it to the shared message shape.

    Each procmail message carries its body inline (PlainTextBody/HTMLBody), so the
    normalized dict already holds 'text'/'html' for extract_credentials_from_email().
    """
    try:
        resp = requests.get(
            f"{PROCMAIL_API_BASE}/inbox/{requests.utils.quote(address)}",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json() or []
    except Exception as exc:
        print(f"[!] Failed to fetch procmail inbox: {exc}")
        return []

    from email.utils import parseaddr

    messages = []
    for idx, item in enumerate(data):
        raw_sender = item.get("Sender", "") or ""
        from_addr = parseaddr(raw_sender)[1] or raw_sender
        text_body = _decode_qp(item.get("PlainTextBody", "") or "")
        html_body = _decode_qp(item.get("HTMLBody") or "")
        messages.append({
            "id": f"{item.get('ReceivedAt', idx)}-{idx}",
            "subject": _decode_mime_header(item.get("Subject", "")),
            "from": {"address": from_addr},
            "text": text_body,
            "html": [html_body] if html_body else [],
        })
    return messages


def _wait_for_credentials_email_procmail(address, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """Poll the procmail inbox until the credentials email arrives."""
    print(f"[*] Waiting for credentials email (max {max_wait_seconds}s / {max_wait_seconds//60} minutes)...")
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"[*] Checking procmail inbox (attempt {attempt}, {remaining}s remaining)...")

        messages = get_procmail_messages(address)
        # Bodies are inline, so fetch_full is the identity function.
        result = _scan_messages_for_credentials(messages, fetch_full=lambda m: m)
        if result:
            return result

        if messages:
            print(f"[*] Found {len(messages)} email(s), but credentials email not yet received")
        else:
            print("[*] Inbox is empty")

        print(f"[*] Waiting {EMAIL_POLL_SECONDS}s before next check...")
        time.sleep(EMAIL_POLL_SECONDS)

    print(f"[!] Timeout: Credentials email not received after {max_wait_seconds}s")
    return None


# ═══════════════════════════════════════════════════════════
# tmaily.com email backend — same-origin REST API behind the SPA
#
# tmaily.com's front-end talks to its own origin:
#   GET /generate                    -> {"address": "xxx@10timer.com"}; the
#                                       TMaily_sid session cookie binds the
#                                       inbox, so all calls must share one
#                                       requests.Session.
#   GET /emails?address={addr}       -> JSON array of message summaries
#                                       ({id, subject, from, date, ...}).
#   GET /emails?address={addr}&id={id} / GET /message/{id}
#                                    -> fallbacks for the full body when the
#                                       list entry doesn't carry it inline.
# The site loads Cloudflare Turnstile but /generate works without a token;
# when it doesn't, the API answers with an error mentioning "turnstile".
# ═══════════════════════════════════════════════════════════
_TMAILY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_tmaily_http = None


def _tmaily_session():
    """Lazily create the shared requests.Session carrying the TMaily_sid cookie."""
    global _tmaily_http
    if _tmaily_http is None:
        _tmaily_http = requests.Session()
        _tmaily_http.headers.update({"User-Agent": _TMAILY_UA})
        if IPTVV_PROXY_URL:
            # tmaily.com is Cloudflare-fronted and appears to block the same
            # Apify datacenter IP that iptvv.ca blocks (see IPTVV_PROXY_URL
            # comment above) — route through the residential proxy so tmaily
            # sees the same egress IP as the browser.
            _tmaily_http.proxies = {"http": IPTVV_PROXY_URL, "https": IPTVV_PROXY_URL}
            _tmaily_http.verify = False
            try:
                import urllib3
                urllib3.disable_warnings()
            except Exception:
                pass
    return _tmaily_http


def _tmaily_available_domains():
    """Return tmaily's current mintable domains, or None if the check itself failed."""
    try:
        resp = _tmaily_session().get(f"{TMAILY_API_BASE}/domains", timeout=10)
        resp.raise_for_status()
        data = resp.json() or {}
        domains = data.get("domains") if isinstance(data, dict) else data
        return list(domains) if isinstance(domains, list) else None
    except Exception as exc:
        print(f"[!] Could not fetch tmaily domain list: {exc}")
        return None


def create_tmaily_inbox():
    """Generate a disposable address via tmaily.com.

    Returns the email address string, or None on failure. Passes force=true so
    every call mints a fresh address instead of returning the session's
    current one, and pins the address to TMAILY_DOMAIN when set. tmaily rotates
    its domain roster over time, so a configured TMAILY_DOMAIN that's since been
    retired (server answers "Invalid domain selected.") falls back to a
    tmaily-assigned domain instead of hard-failing the run.
    """
    try:
        domain = TMAILY_DOMAIN
        if domain:
            available = _tmaily_available_domains()
            if available is not None and domain not in available:
                print(f"[!] TMAILY_DOMAIN {domain!r} is no longer offered by tmaily.com "
                      f"(current domains: {available}); falling back to a rotating domain")
                domain = ""
        params = {"force": "true"}
        if domain:
            params["domain"] = domain
        resp = _tmaily_session().get(
            f"{TMAILY_API_BASE}/generate", params=params, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "turnstile" in str(data.get("error", "")).lower():
            raise RuntimeError("tmaily.com now requires a Turnstile token for /generate")
        address = (data or {}).get("address", "")
        if "@" not in address:
            raise RuntimeError(f"unexpected /generate response: {str(data)[:120]!r}")
        if domain and not address.endswith("@" + domain):
            raise RuntimeError(
                f"tmaily returned {address!r}, not on requested domain {domain}"
            )
        print(f"[OK] tmaily inbox created: {address}")
        return address
    except Exception as exc:
        print(f"[!] Failed to create tmaily inbox: {exc}")
        return None


def _tmaily_normalize_message(item):
    """Map a raw tmaily message dict onto the shared normalized shape."""
    from email.utils import parseaddr

    raw_from = item.get("from", "")
    if isinstance(raw_from, dict):
        from_addr = raw_from.get("address", "") or raw_from.get("email", "")
    else:
        from_addr = parseaddr(str(raw_from))[1] or str(raw_from)

    text_body = ""
    html_body = ""
    for key in ("text", "textBody", "text_body", "plainText"):
        if item.get(key):
            text_body = _decode_qp(str(item[key]))
            break
    for key in ("html", "htmlBody", "html_body", "body", "content"):
        if item.get(key):
            value = item[key]
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            html_body = _decode_qp(str(value))
            break

    return {
        "id": str(item.get("id", "")),
        "subject": _decode_mime_header(item.get("subject", "")),
        "from": {"address": from_addr},
        "text": text_body,
        "html": [html_body] if html_body else [],
    }


def get_tmaily_messages(address):
    """Fetch the tmaily inbox, normalized to the shared message shape."""
    try:
        resp = _tmaily_session().get(
            f"{TMAILY_API_BASE}/emails",
            params={"address": address},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json() or []
    except Exception as exc:
        print(f"[!] Failed to fetch tmaily inbox: {exc}")
        return []
    if not isinstance(data, list):
        data = data.get("emails", []) if isinstance(data, dict) else []
    return [_tmaily_normalize_message(item) for item in data]


def _tmaily_fetch_full(address, msg):
    """Return a message with a body, fetching it separately if the list entry had none.

    Tries GET /emails?address=&id= first, then falls back to scraping the
    server-rendered /message/{id} page; extract_credentials_from_email()
    strips HTML tags itself, so raw page HTML is an acceptable body.
    """
    if msg.get("text") or msg.get("html"):
        return msg

    session = _tmaily_session()
    msg_id = msg.get("id", "")
    try:
        resp = session.get(
            f"{TMAILY_API_BASE}/emails",
            params={"address": address, "id": msg_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            full = _tmaily_normalize_message(data)
            if full.get("text") or full.get("html"):
                full["subject"] = full["subject"] or msg.get("subject", "")
                full["from"] = full["from"] if full["from"]["address"] else msg.get("from", {})
                return full
    except Exception as exc:
        print(f"[!] Failed to fetch tmaily message {msg_id} by id: {exc}")

    try:
        resp = session.get(f"{TMAILY_API_BASE}/message/{msg_id}", timeout=15)
        resp.raise_for_status()
        page = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", resp.text, flags=re.I | re.S)
        return {**msg, "html": [page]}
    except Exception as exc:
        print(f"[!] Failed to fetch tmaily message page for {msg_id}: {exc}")
    return msg


def _wait_for_credentials_email_tmaily(address, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """Poll the tmaily inbox until the credentials email arrives."""
    print(f"[*] Waiting for credentials email (max {max_wait_seconds}s / {max_wait_seconds//60} minutes)...")
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"[*] Checking tmaily inbox (attempt {attempt}, {remaining}s remaining)...")

        messages = get_tmaily_messages(address)
        result = _scan_messages_for_credentials(
            messages, fetch_full=lambda m: _tmaily_fetch_full(address, m)
        )
        if result:
            return result

        if messages:
            print(f"[*] Found {len(messages)} email(s), but credentials email not yet received")
        else:
            print("[*] Inbox is empty")

        print(f"[*] Waiting {EMAIL_POLL_SECONDS}s before next check...")
        time.sleep(EMAIL_POLL_SECONDS)

    print(f"[!] Timeout: Credentials email not received after {max_wait_seconds}s")
    return None


# ═══════════════════════════════════════════════════════════
# mail.cx email backend — api.mail.cx v1 REST API
#
# Mailboxes are implicit: any local-part @ a system domain is live the
# instant a mail lands, nothing to "create". Reading it requires an account
# API token (MAILCX_API_TOKEN, minted from the mail.cx dashboard -> Tokens);
# anonymous requests get 401 authentication_required.
#   GET /v1/config                   -> {"system_domains": [...], ...} (public)
#   GET /v1/inbox/{address}          -> long-polls up to 25s server-side;
#                                       200 with new mail, or 204 if none
#                                       arrived in that window (just reconnect).
#   GET /v1/email/{id}               -> full parsed body (text/html).
# ═══════════════════════════════════════════════════════════
_mailcx_http = None
_mailcx_domain_cache = None


def _mailcx_session():
    """Lazily create the shared requests.Session carrying the API token."""
    global _mailcx_http
    if _mailcx_http is None:
        _mailcx_http = requests.Session()
        _mailcx_http.headers.update({"x-api-token": MAILCX_API_TOKEN})
        if IPTVV_PROXY_URL:
            # api.mail.cx is Cloudflare-fronted like tmaily.com and iptvv.ca;
            # route through the residential proxy so it doesn't see the
            # (likely blocked) Apify datacenter IP.
            _mailcx_http.proxies = {"http": IPTVV_PROXY_URL, "https": IPTVV_PROXY_URL}
            _mailcx_http.verify = False
            try:
                import urllib3
                urllib3.disable_warnings()
            except Exception:
                pass
    return _mailcx_http


def _mailcx_pick_domain():
    """Return the domain to mint addresses on, validating MAILCX_DOMAIN live.

    Caches the result for the process lifetime (one run == one inbox).
    """
    global _mailcx_domain_cache
    if _mailcx_domain_cache:
        return _mailcx_domain_cache

    domain = MAILCX_DOMAIN
    try:
        resp = _mailcx_session().get(f"{MAILCX_API_BASE}/config", timeout=15)
        resp.raise_for_status()
        domains = (resp.json() or {}).get("system_domains", [])
        names = [d.get("domain") for d in domains if d.get("domain")]
        if domain and domain not in names:
            print(f"[!] MAILCX_DOMAIN {domain!r} is not in mail.cx's system domains "
                  f"{names}; falling back to the default one")
            domain = ""
        if not domain:
            default = next((d["domain"] for d in domains if d.get("default")), None)
            domain = default or (names[0] if names else "")
    except Exception as exc:
        print(f"[!] Could not fetch mail.cx domain list: {exc}")

    _mailcx_domain_cache = domain or MAILCX_DOMAIN or "uqu.me"
    return _mailcx_domain_cache


def verify_mailcx_token():
    """Check MAILCX_API_TOKEN before spending a browser session on it.

    GET /v1/tokens is a plain (non-long-poll) authenticated call, so this
    returns immediately instead of waiting out a 25s long-poll hold.

    Returns True if the token works, False if mail.cx rejects the account
    (401/402/403, e.g. quota exhausted or plan lapsed) so the caller can fall
    back to another backend. Raises on a missing token or network failure.
    """
    if not MAILCX_API_TOKEN:
        raise RuntimeError(
            "MAILCX_API_TOKEN is not set; the mailcx backend needs an API token "
            "from the mail.cx dashboard (Dashboard -> Tokens)"
        )
    try:
        resp = _mailcx_session().get(f"{MAILCX_API_BASE}/tokens", timeout=15)
    except Exception as exc:
        raise RuntimeError(f"mail.cx API token check failed: {exc}") from exc
    if resp.status_code in (401, 402, 403):
        print(f"[!] mail.cx rejected the API token (HTTP {resp.status_code}): "
              f"{resp.text[:200].strip()}")
        return False
    try:
        resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"mail.cx API token check failed: {exc}") from exc
    print("[OK] mail.cx API token verified")
    return True


def create_mailcx_inbox():
    """Mint a disposable address on mail.cx.

    Nothing is actually created server-side (mail.cx mailboxes are implicit) —
    this just picks a live system domain and a random local-part. Returns the
    address, or None if MAILCX_API_TOKEN is missing or the domain lookup fails.
    """
    try:
        if not MAILCX_API_TOKEN:
            raise RuntimeError(
                "MAILCX_API_TOKEN is not set; the mailcx backend needs an API "
                "token from the mail.cx dashboard (Dashboard -> Tokens)"
            )
        domain = _mailcx_pick_domain()
        if not domain:
            raise RuntimeError("could not determine a mail.cx domain to use")
        local_part = ''.join(random.choices(string.ascii_lowercase + string.digits, k=14))
        address = f"{local_part}@{domain}"
        print(f"[OK] mailcx inbox ready: {address}")
        return address
    except Exception as exc:
        print(f"[!] Failed to create mailcx inbox: {exc}")
        return None


def _mailcx_normalize_message(item):
    """Map a raw mail.cx message dict onto the shared normalized shape."""
    from email.utils import parseaddr

    raw_from = item.get("from_email", item.get("from", ""))
    if isinstance(raw_from, dict):
        from_addr = raw_from.get("address", "") or raw_from.get("email", "")
    else:
        from_addr = parseaddr(str(raw_from))[1] or str(raw_from)

    # "preview_text" (list endpoint) is deliberately excluded here: it's a
    # truncated preview, and if it landed in "text" it would make
    # _mailcx_fetch_full's already-have-a-body short-circuit skip fetching
    # the real, complete body from GET /v1/email/<id>.
    text_body = ""
    html_body = ""
    for key in ("text", "textBody", "text_body", "plainText"):
        if item.get(key):
            text_body = _decode_qp(str(item[key]))
            break
    for key in ("html", "htmlBody", "html_body", "body", "content"):
        if item.get(key):
            value = item[key]
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            html_body = _decode_qp(str(value))
            break

    return {
        "id": str(item.get("id", "")),
        "subject": _decode_mime_header(item.get("subject", "")),
        "from": {"address": from_addr},
        "text": text_body,
        "html": [html_body] if html_body else [],
    }


def get_mailcx_messages(address, since=None):
    """Long-poll the mail.cx inbox once; returns (messages, next_since).

    The request itself blocks server-side for up to ~25s. Uses a generous
    client timeout so that hold isn't mistaken for a network failure.
    """
    try:
        params = {"since": since} if since else {}
        resp = _mailcx_session().get(
            f"{MAILCX_API_BASE}/inbox/{requests.utils.quote(address)}",
            params=params,
            timeout=35,
        )
        if resp.status_code == 204:
            return [], since
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as exc:
        print(f"[!] Failed to fetch mailcx inbox: {exc}")
        return [], since
    emails = data.get("emails", []) if isinstance(data, dict) else []
    next_since = data.get("next_since", since) if isinstance(data, dict) else since
    return [_mailcx_normalize_message(item) for item in emails], next_since


def _mailcx_fetch_full(msg):
    """Fetch the full parsed body for a message by id."""
    if msg.get("text") or msg.get("html"):
        return msg
    msg_id = msg.get("id", "")
    try:
        resp = _mailcx_session().get(f"{MAILCX_API_BASE}/email/{msg_id}", timeout=15)
        resp.raise_for_status()
        data = resp.json() or {}
        full = _mailcx_normalize_message(data)
        full["subject"] = full["subject"] or msg.get("subject", "")
        full["from"] = full["from"] if full["from"]["address"] else msg.get("from", {})
        return full
    except Exception as exc:
        print(f"[!] Failed to fetch mailcx message {msg_id}: {exc}")
        return msg


def _wait_for_credentials_email_mailcx(address, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """Long-poll the mail.cx inbox until the credentials email arrives.

    Each call already blocks server-side for up to ~25s, so no extra sleep is
    needed between iterations on a plain empty result.
    """
    print(f"[*] Waiting for credentials email (max {max_wait_seconds}s / {max_wait_seconds//60} minutes)...")
    deadline = time.time() + max_wait_seconds
    attempt = 0
    since = None
    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"[*] Checking mailcx inbox (attempt {attempt}, {remaining}s remaining)...")

        messages, next_since = get_mailcx_messages(address, since=since)
        since = next_since or since
        result = _scan_messages_for_credentials(messages, fetch_full=_mailcx_fetch_full)
        if result:
            return result

        if messages:
            print(f"[*] Found {len(messages)} email(s), but credentials email not yet received")
        else:
            print("[*] Inbox is empty (long-poll cycle ended, reconnecting)")

    print(f"[!] Timeout: Credentials email not received after {max_wait_seconds}s")
    return None


# ═══════════════════════════════════════════════════════════
# Gmail plus-addressing backend — real inbox polled over IMAP
#
# Each run checks out with a unique alias like wdmonitoring+55455@gmail.com;
# Gmail delivers everything to the base IPTVV_GMAIL_ADDRESS inbox, which we
# poll over IMAP using a Google app password (IPTVV_GMAIL_APP_PASSWORD).
# Unlike disposable-mail domains, a real Gmail address is never dropped by
# the store's mailer.
# ═══════════════════════════════════════════════════════════
def _gmail_imap_login():
    """Open an authenticated IMAP connection to the Gmail account."""
    if not IPTVV_GMAIL_APP_PASSWORD:
        raise RuntimeError(
            f"IPTVV_GMAIL_APP_PASSWORD is not set; the gmail backend needs a "
            f"Google app password for {IPTVV_GMAIL_ADDRESS}"
        )
    imap = imaplib.IMAP4_SSL(IPTVV_GMAIL_IMAP_HOST)
    imap.login(IPTVV_GMAIL_ADDRESS, IPTVV_GMAIL_APP_PASSWORD)
    return imap


def verify_gmail_login():
    """Raise early with a clear error if the Gmail IMAP login is broken."""
    try:
        imap = _gmail_imap_login()
        try:
            imap.logout()
        except Exception:
            pass
        print(f"[OK] Gmail IMAP login verified for {IPTVV_GMAIL_ADDRESS}")
    except Exception as exc:
        raise RuntimeError(
            f"Gmail IMAP login failed for {IPTVV_GMAIL_ADDRESS}: {exc}. "
            "IPTVV_GMAIL_APP_PASSWORD must be a Google app password "
            "(myaccount.google.com/apppasswords, requires 2-Step Verification), "
            "not the regular account password."
        ) from exc


_ALIAS_FIRST_NAMES = [
    "emma", "liam", "olivia", "noah", "ava", "lucas", "mia", "ethan", "chloe", "jacob",
    "sophie", "nathan", "julia", "samuel", "laura", "daniel", "sarah", "thomas", "megan", "ryan",
]
_ALIAS_LAST_NAMES = [
    "smith", "tremblay", "brown", "martin", "roy", "wilson", "gagnon", "taylor", "campbell",
    "anderson", "lee", "white", "thompson", "clark", "walker", "young", "scott", "king",
]


def _random_person():
    """A random (first, last) name pair used for the alias and billing name."""
    return random.choice(_ALIAS_FIRST_NAMES), random.choice(_ALIAS_LAST_NAMES)


def create_gmail_alias(first=None, last=None):
    """Build a fresh receiving address and verify the IMAP login up front.

    With IPTVV_GMAIL_ALIAS_DOMAIN set, returns a realistic random address on
    that catch-all domain (e.g. emma.taylor4821@domain); otherwise a unique
    plus-addressed alias of IPTVV_GMAIL_ADDRESS. Either way the mail lands in
    IPTVV_GMAIL_ADDRESS. Returns None on failure.
    """
    try:
        if "@" not in IPTVV_GMAIL_ADDRESS:
            raise RuntimeError(f"invalid IPTVV_GMAIL_ADDRESS: {IPTVV_GMAIL_ADDRESS!r}")
        imap = _gmail_imap_login()
        try:
            imap.logout()
        except Exception:
            pass
        if IPTVV_GMAIL_ALIAS_DOMAIN:
            if not (first and last):
                first, last = _random_person()
            sep = random.choice([".", "", "_"])
            digits = str(random.randint(10, 9999))
            alias = f"{first}{sep}{last}{digits}@{IPTVV_GMAIL_ALIAS_DOMAIN}"
        else:
            local, domain = IPTVV_GMAIL_ADDRESS.split("@", 1)
            suffix = f"{int(time.time())}{random.randint(100, 999)}"
            alias = f"{local}+{suffix}@{domain}"
        print(f"[OK] Gmail alias created: {alias}")
        return alias
    except Exception as exc:
        print(f"[!] Failed to prepare Gmail alias: {exc}")
        return None


def _gmail_message_to_dict(raw_bytes, msg_id):
    """Parse a raw RFC822 message into the shared normalized message shape."""
    import email as email_lib
    from email.utils import parseaddr

    msg = email_lib.message_from_bytes(raw_bytes)
    subject = _decode_mime_header(msg.get("Subject", ""))
    from_addr = parseaddr(msg.get("From", ""))[1]

    text_body, html_body = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            body = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain" and not text_body:
            text_body = body
        elif ctype == "text/html" and not html_body:
            html_body = body

    return {
        "id": msg_id,
        "subject": subject,
        "from": {"address": from_addr},
        "text": text_body,
        "html": [html_body] if html_body else [],
    }


def get_gmail_messages(alias):
    """Fetch messages addressed to the alias (INBOX + Spam), normalized.

    Bodies come back inline, so fetch_full stays the identity function like
    the procmail backend.
    """
    try:
        imap = _gmail_imap_login()
    except Exception as exc:
        print(f"[!] Failed to connect to Gmail IMAP: {exc}")
        return []

    messages = []
    try:
        for folder in ('INBOX', '"[Gmail]/Spam"'):
            try:
                status, _ = imap.select(folder, readonly=True)
                if status != "OK":
                    continue
                status, data = imap.search(None, "TO", f'"{alias}"')
                if status != "OK" or not data or not data[0]:
                    continue
                for num in data[0].split():
                    status, fetched = imap.fetch(num, "(RFC822)")
                    if status != "OK" or not fetched or not fetched[0]:
                        continue
                    messages.append(
                        _gmail_message_to_dict(fetched[0][1], f"{folder}-{num.decode()}")
                    )
            except Exception as exc:
                print(f"[!] Failed to read Gmail folder {folder}: {exc}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return messages


def _wait_for_credentials_email_gmail(alias, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """Poll the Gmail inbox over IMAP until the credentials email arrives."""
    print(f"[*] Waiting for credentials email (max {max_wait_seconds}s / {max_wait_seconds//60} minutes)...")
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"[*] Checking gmail inbox (attempt {attempt}, {remaining}s remaining)...")

        messages = get_gmail_messages(alias)
        result = _scan_messages_for_credentials(messages, fetch_full=lambda m: m)
        if result:
            return result

        if messages:
            print(f"[*] Found {len(messages)} email(s), but credentials email not yet received")
        else:
            print("[*] Inbox is empty")

        print(f"[*] Waiting {EMAIL_POLL_SECONDS}s before next check...")
        time.sleep(EMAIL_POLL_SECONDS)

    print(f"[!] Timeout: Credentials email not received after {max_wait_seconds}s")
    return None


# ═══════════════════════════════════════════════════════════
# Boomlify backend — v1.boomlify.com, free web guest flow (no key, no captcha)
#
# Guest mode (default, what the boomlify.com web app does):
#   POST /guest/init                  -> {"token": <JWT>, "isGuest": true}
#   GET  /domains/public              -> [{"id", "domain", "is_premium", ...}]
#   POST /emails/create               -> {"email": "x@dom", "domainId"} => {"id", "email", "expires_at"}
#   GET  /emails/{id}/received        -> {"data": [{"id", "subject", "from_email",
#                                         "body_text", "body_html", ...}], "metadata": {...}}
#   Guest responses are XOR-obfuscated as {"encrypted": "<hex>"}; the key is
#   picked by the x-enc-key-id response header from a key ring shipped in the
#   site's main JS bundle, which is fetched at runtime so key rotations don't
#   break us. Guest inboxes live ~2 months.
# API mode (only when BOOMLIFY_API_KEY is set; costs credits per inbox):
#   POST /api/v1/emails/create?time=..  GET /api/v1/emails/{id}/messages
# ═══════════════════════════════════════════════════════════
BOOMLIFY_WEB_BASE = "https://boomlify.com"
# Fallback XOR key if the bundle can't be fetched/parsed.
_BOOMLIFY_DEFAULT_KEY = "7a9b3c8d2e1f4g5h6i9j0k8l2m4n6o8p"
_boomlify_keys_cache = None
# Domains IPTVV rejected during this run; never offered again.
_boomlify_rejected_domains = set()


def _html_to_text(fragment):
    fragment = re.sub(r"(?is)<(script|style).*?</\1>", " ", fragment)
    fragment = re.sub(r"(?s)<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(fragment)).strip()


def _boomlify_session():
    http = requests.Session()
    http.headers.update({
        "Accept": "application/json",
        "User-Agent": get_random_user_agent(),
        "Origin": BOOMLIFY_WEB_BASE,
        "Referer": f"{BOOMLIFY_WEB_BASE}/",
    })
    if BOOMLIFY_API_KEY:
        http.headers["X-API-Key"] = BOOMLIFY_API_KEY
    return http


def _boomlify_keys(http):
    """Return (default_key, key_ring) scraped from boomlify.com's main JS bundle."""
    global _boomlify_keys_cache
    if _boomlify_keys_cache is None:
        default, ring = _BOOMLIFY_DEFAULT_KEY, {}
        try:
            home = http.get(f"{BOOMLIFY_WEB_BASE}/en", timeout=30).text
            bundle = re.search(r'src="(/assets/main\.[^"]+\.js)"', home)
            js = http.get(f"{BOOMLIFY_WEB_BASE}{bundle.group(1)}", timeout=30).text
            found = re.search(r'keyString="([^"]+)"', js)
            if found:
                default = found.group(1)
            ring_js = re.search(r"getTransportKeyRing\(\)\{try\{const e=JSON\.parse\('(\{.*?\})'\)", js)
            if ring_js:
                ring = json.loads(ring_js.group(1))
        except Exception as exc:
            print(f"[!] Boomlify: could not load decryption keys from the site bundle ({exc}); "
                  "using the built-in key")
        _boomlify_keys_cache = (default, ring)
    return _boomlify_keys_cache


def _boomlify_payload(http, resp):
    """Decode a Boomlify response: undo the guest XOR layer and any "data" envelope."""
    body = resp.json()
    if isinstance(body, dict) and "encrypted" in body:
        default, ring = _boomlify_keys(http)
        key = ring.get(resp.headers.get("x-enc-key-id") or "", default).encode()
        raw = bytes.fromhex(body["encrypted"])
        body = json.loads("".join(chr(b ^ key[i % len(key)]) for i, b in enumerate(raw)))
    return body


def verify_boomlify_key():
    """Pre-flight: guest mode needs nothing; API mode checks BOOMLIFY_API_KEY.

    Returns False if Boomlify rejects the key (401/402/403/429, e.g. out of
    credits or daily limit hit) so the caller can fall back to another backend.
    """
    if not BOOMLIFY_API_KEY:
        return True
    try:
        resp = _boomlify_session().get(f"{BOOMLIFY_API_BASE}/api/v1/account/credits", timeout=15)
    except Exception as exc:
        raise RuntimeError(f"Boomlify API key check failed: {exc}") from exc
    if resp.status_code in (401, 402, 403, 429):
        print(f"[!] Boomlify rejected the API key (HTTP {resp.status_code}): "
              f"{resp.text[:200].strip()}")
        return False
    if not resp.ok:
        raise RuntimeError(f"Boomlify API key check returned HTTP {resp.status_code}: "
                           f"{resp.text[:200].strip()}")
    return True


def _create_boomlify_guest_inbox(http):
    resp = http.post(f"{BOOMLIFY_API_BASE}/guest/init", json={}, timeout=30)
    resp.raise_for_status()
    init = _boomlify_payload(http, resp)
    if not init.get("token"):
        raise RuntimeError(f"no guest token in /guest/init response: {init}")
    if init.get("guestFirstMailboxCaptchaEnabled"):
        print("[!] Boomlify now asks guests for a captcha on the first mailbox; creation may fail")
    http.headers["Authorization"] = f"Bearer {init['token']}"

    resp = http.get(f"{BOOMLIFY_API_BASE}/domains/public", timeout=30)
    resp.raise_for_status()
    skip = BOOMLIFY_BLOCKED_DOMAINS | _boomlify_rejected_domains
    domains = [d for d in _boomlify_payload(http, resp)
               if d.get("is_active", 1) and not d.get("is_premium")
               and (d.get("domain") or "").lower() not in skip]
    if not domains:
        raise RuntimeError(f"no usable public domains (skipped: {', '.join(sorted(skip))})")
    by_name = {d["domain"].lower(): d for d in domains}
    preferred = [by_name[name] for name in BOOMLIFY_DOMAINS if name in by_name]
    if preferred:
        domain = preferred[0]
    else:
        # The domain that passed IPTVV's filter is a subdomain, so try those first.
        subdomains = [d for d in domains if d["domain"].count(".") >= 2]
        domain = random.choice(subdomains or domains)

    local = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    resp = http.post(f"{BOOMLIFY_API_BASE}/emails/create",
                     json={"email": f"{local}@{domain['domain']}", "domainId": domain["id"]},
                     timeout=30)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300].strip()}")
    created = _boomlify_payload(http, resp)
    return created.get("email"), created.get("id"), created.get("expires_at")


def _create_boomlify_api_inbox(http):
    params = {"time": BOOMLIFY_EMAIL_TIME}
    if BOOMLIFY_DOMAIN:
        params["domain"] = BOOMLIFY_DOMAIN
    resp = http.post(f"{BOOMLIFY_API_BASE}/api/v1/emails/create", params=params, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300].strip()}")
    body = _boomlify_payload(http, resp)
    email = (body.get("data") or body).get("email") or {}
    return email.get("address"), email.get("id"), email.get("expires_at")


def create_boomlify_inbox():
    """Create a Boomlify address; returns (address, email_id, http) or (None, None, None)."""
    mode = "api" if BOOMLIFY_API_KEY else "guest"
    http = _boomlify_session()
    try:
        create = _create_boomlify_api_inbox if mode == "api" else _create_boomlify_guest_inbox
        address, email_id, expires_at = create(http)
        if not address or not email_id:
            print(f"[!] Boomlify ({mode}): no address/id in create response")
            return None, None, None
        print(f"[OK] Boomlify inbox ({mode}): {address} (id={email_id}, expires {expires_at})")
        return address, email_id, http
    except Exception as exc:
        print(f"[!] Failed to create Boomlify inbox ({mode}): {exc}")
        return None, None, None


def _boomlify_normalize_message(item):
    body_html = item.get("body_html") or ""
    return {
        "id": item.get("id"),
        "subject": item.get("subject") or "",
        "from": {"address": item.get("from_email") or item.get("from") or ""},
        "text": item.get("body_text") or _html_to_text(body_html),
        "html": [body_html] if body_html else [],
    }


def get_boomlify_messages(http, email_id):
    """Fetch the inbox once; returns normalized messages (bodies included)."""
    try:
        if BOOMLIFY_API_KEY:
            resp = http.get(f"{BOOMLIFY_API_BASE}/api/v1/emails/{email_id}/messages",
                            params={"limit": 50}, timeout=30)
        else:
            resp = http.get(f"{BOOMLIFY_API_BASE}/emails/{email_id}/received",
                            params={"page": 1, "limit": 50, "skipCache": "true"}, timeout=30)
        resp.raise_for_status()
        body = _boomlify_payload(http, resp)
    except Exception as exc:
        print(f"[!] Failed to fetch Boomlify inbox: {exc}")
        return []
    if isinstance(body, dict):
        data = body.get("data", body)
        items = data.get("messages") if isinstance(data, dict) else data
    else:
        items = body
    return [_boomlify_normalize_message(item) for item in (items or []) if isinstance(item, dict)]


def _wait_for_credentials_email_boomlify(http, email_id, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """Poll the Boomlify inbox until the credentials email arrives."""
    print(f"[*] Waiting for credentials email (max {max_wait_seconds}s / {max_wait_seconds//60} minutes)...")
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"[*] Checking Boomlify inbox (attempt {attempt}, {remaining}s remaining)...")
        messages = get_boomlify_messages(http, email_id)
        # Boomlify returns full bodies in the listing, so no per-message fetch.
        result = _scan_messages_for_credentials(messages, fetch_full=lambda msg: msg)
        if result:
            return result
        if messages:
            print(f"[*] Found {len(messages)} email(s), but credentials email not yet received")
        else:
            print("[*] Inbox is empty")
        time.sleep(min(EMAIL_POLL_SECONDS, max(0, deadline - time.time())))

    print(f"[!] Timeout: Credentials email not received after {max_wait_seconds}s")
    return None


# ═══════════════════════════════════════════════════════════
# Email backend dispatchers (backend-agnostic entry points used by main())
# ═══════════════════════════════════════════════════════════
def email_backend_chain():
    """Primary backend followed by the configured fallbacks, deduplicated."""
    chain = []
    for backend in [IPTVV_EMAIL_BACKEND] + IPTVV_EMAIL_FALLBACKS:
        if backend not in chain:
            chain.append(backend)
    return chain


def email_backend_ready(backend):
    """Run the backend's credential pre-flight; False means skip it.

    Cheap checks only, run before the browser starts so a broken inbox
    doesn't waste a checkout + captcha.
    """
    try:
        if backend == "gmail":
            verify_gmail_login()
        elif backend == "mailcx":
            return verify_mailcx_token()
        elif backend == "boomlify":
            return verify_boomlify_key()
    except Exception as exc:
        print(f"[!] {backend} backend pre-flight failed: {exc}")
        return False
    return True


def _create_session_for(backend):
    """Allocate a receiving address on one backend; None on failure."""
    if backend == "mailtm":
        address, password, auth_token = create_mailtm_account()
        if not address:
            return None
        return {"backend": "mailtm", "address": address, "password": password, "token": auth_token}

    if backend == "procmail":
        address = create_procmail_inbox()
        if not address:
            return None
        return {"backend": "procmail", "address": address}

    if backend == "gmail":
        first, last = _random_person()
        alias = create_gmail_alias(first, last)
        if not alias:
            return None
        session = {"backend": "gmail", "address": alias}
        if IPTVV_GMAIL_ALIAS_DOMAIN:
            # Billing name matches the address's name for a natural-looking order.
            session["names"] = {"first_name": first.capitalize(), "last_name": last.capitalize()}
        return session

    if backend == "mailcx":
        address = create_mailcx_inbox()
        if not address:
            return None
        return {"backend": "mailcx", "address": address}

    if backend == "boomlify":
        address, email_id, http = create_boomlify_inbox()
        if not address:
            return None
        return {"backend": "boomlify", "address": address, "email_id": email_id, "http": http}

    # Default: tmaily.com disposable inbox.
    address = create_tmaily_inbox()
    if not address:
        return None
    return {"backend": "tmaily", "address": address}


def create_email_session(backends=None):
    """Allocate a receiving address on the first backend that succeeds.

    'backends' defaults to email_backend_chain(). Returns a session dict that
    always carries 'backend' and 'address', or None if every backend failed.
    """
    for backend in backends if backends is not None else email_backend_chain():
        session = _create_session_for(backend)
        if session:
            return session
        print(f"[!] {backend} backend could not allocate an inbox, trying next backend")
    return None


def wait_for_credentials_email(driver, session, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """Dispatch inbox polling to the configured backend."""
    backend = (session or {}).get("backend")
    if backend == "mailtm":
        return _wait_for_credentials_email_mailtm(session["token"], max_wait_seconds)
    if backend == "procmail":
        return _wait_for_credentials_email_procmail(session["address"], max_wait_seconds)
    if backend == "gmail":
        return _wait_for_credentials_email_gmail(session["address"], max_wait_seconds)
    if backend == "mailcx":
        return _wait_for_credentials_email_mailcx(session["address"], max_wait_seconds)
    if backend == "boomlify":
        return _wait_for_credentials_email_boomlify(session["http"], session["email_id"], max_wait_seconds)
    return _wait_for_credentials_email_tmaily(session["address"], max_wait_seconds)


# ═══════════════════════════════════════════════════════════
# Browser Automation (Puppeteer worker: browser/checkout.js)
# ═══════════════════════════════════════════════════════════

def get_random_user_agent():
    """Generate a random realistic user agent."""
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    ]
    return random.choice(user_agents)


# Holds the running local forwarding proxy so its listener thread isn't GC'd.
_local_proxy = None


class _ForwardingProxy:
    """A local no-auth HTTP/HTTPS proxy that chains to an authenticated upstream.

    Chrome's in-browser proxy auth (extension onAuthRequired) is unreliable under
    undetected-chromedriver / the regular-driver fallback and yields empty pages.
    Instead we run this tiny proxy on 127.0.0.1 with no auth, point Chrome at it,
    and inject the upstream's Proxy-Authorization here. This mirrors how `requests`
    reaches the site successfully through the same upstream proxy.
    """

    def __init__(self, upstream_url):
        from urllib.parse import urlparse

        parsed = urlparse(upstream_url)
        self.up_host = parsed.hostname
        self.up_port = parsed.port
        self.auth_header = None
        if parsed.username:
            cred = f"{parsed.username}:{parsed.password or ''}"
            self.auth_header = base64.b64encode(cred.encode()).decode()

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(128)
        self.port = self.server.getsockname()[1]

    def start(self):
        threading.Thread(target=self._serve, daemon=True).start()
        return self.port

    def _serve(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        upstream = None
        try:
            header = b""
            while b"\r\n\r\n" not in header:
                chunk = client.recv(4096)
                if not chunk:
                    client.close()
                    return
                header += chunk
            head, _, rest = header.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            request_line = lines[0].decode("latin1")
            method = request_line.split(" ", 1)[0].upper()

            upstream = socket.create_connection((self.up_host, self.up_port), timeout=30)

            if method == "CONNECT":
                target = request_line.split(" ")[1]
                req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                if self.auth_header:
                    req += f"Proxy-Authorization: Basic {self.auth_header}\r\n"
                req += "\r\n"
                upstream.sendall(req.encode())
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                # Relay the upstream's CONNECT status line back to the client.
                client.sendall(resp.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:
                # Plain HTTP: strip any client proxy-auth, inject ours, forward.
                new_lines = [lines[0]]
                for line in lines[1:]:
                    if line.lower().startswith(b"proxy-authorization:"):
                        continue
                    new_lines.append(line)
                if self.auth_header:
                    new_lines.append(f"Proxy-Authorization: Basic {self.auth_header}".encode())
                upstream.sendall(b"\r\n".join(new_lines) + b"\r\n\r\n" + rest)

            self._tunnel(client, upstream)
        except Exception:
            for sock in (client, upstream):
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass

    @staticmethod
    def _tunnel(a, b):
        socks = [a, b]
        try:
            while True:
                readable, _, errored = select.select(socks, [], socks, 120)
                if errored or not readable:
                    break
                for s in readable:
                    other = b if s is a else a
                    try:
                        buf = s.recv(65536)
                    except Exception:
                        return
                    if not buf:
                        return
                    other.sendall(buf)
        finally:
            for s in socks:
                try:
                    s.close()
                except Exception:
                    pass


def get_driver():
    """Start the Puppeteer checkout worker (puppeteer-extra + stealth, real Chrome).

    Returns a ``PuppeteerCheckout`` client, which stands in for the old Selenium
    driver: the checkout helpers below take it as their ``driver`` argument.

    Browser egress goes through IPTVV_PROXY_URL when set (residential proxy) via
    the local no-auth forwarding proxy; otherwise it uses a direct connection.
    """
    global _local_proxy
    proxy_server = None
    if IPTVV_PROXY_URL:
        try:
            _local_proxy = _ForwardingProxy(IPTVV_PROXY_URL)
            port = _local_proxy.start()
            proxy_server = f"http://127.0.0.1:{port}"
            print(f"[*] Routing browser through local forwarding proxy 127.0.0.1:{port} "
                  "-> upstream residential proxy")
        except Exception as exc:
            print(f"[!] Failed to start local forwarding proxy: {exc}; falling back to direct connection")
            proxy_server = None

    client = PuppeteerCheckout(
        proxy_server=proxy_server,
        env_overrides={
            "IPTVV_BASE_URL": IPTVV_BASE_URL,
            "IPTVV_CART_URL": IPTVV_CART_URL,
            "IPTVV_TRIAL_PRODUCT_ID": IPTVV_TRIAL_PRODUCT_ID,
            "IPTVV_PROXY_CHECK_URL": IPTVV_PROXY_CHECK_URL,
            "IPTVV_CLOUDFLARE_WAIT_SECONDS": IPTVV_CLOUDFLARE_WAIT_SECONDS,
            "IPTVV_PAGE_LOAD_RETRIES": IPTVV_PAGE_LOAD_RETRIES,
            "IPTVV_DEBUG_DIR": IPTVV_DEBUG_DIR,
        },
    )
    try:
        print("[*] Starting Puppeteer checkout worker...")
        client.start()
    except WorkerError as exc:
        client.close()
        raise RuntimeError(f"Failed to start Puppeteer browser: {exc}") from exc
    print("[OK] Puppeteer browser initialized")
    return client


def get_public_ip():
    """Best-effort public IP lookup for Cloudflare allowlist/support tickets."""
    try:
        response = requests.get("https://api.ipify.org", timeout=8)
        response.raise_for_status()
        return response.text.strip()
    except Exception as exc:
        print(f"[!] Could not fetch public IP: {exc}")
        return "unknown"


def get_browser_public_ip(driver):
    """Fetch the public IP as seen from inside Chrome (proxied egress)."""
    try:
        return driver.browser_ip() or "unknown"
    except Exception as exc:
        print(f"[!] Could not fetch browser public IP from {IPTVV_PROXY_CHECK_URL}: {exc}")
        return "unknown"


def _translate_worker_error(driver, exc, prefix=""):
    """Map a worker error onto this module's exception types, with IP diagnostics."""
    diag = exc.diag or {}
    artifacts = diag.get("artifacts") or {}
    artifact_text = (f"debug HTML: {artifacts.get('html', 'not saved')}; "
                     f"screenshot: {artifacts.get('screenshot', 'not saved')}")

    if exc.type == "cloudflare_blocked":
        # Container egress (requests, NOT proxied) vs browser egress (goes through
        # IPTVV_PROXY_URL when set). If they match, the residential proxy isn't applied.
        public_ip = get_public_ip()
        browser_ip = get_browser_public_ip(driver)
        print(f"[!] Cloudflare Ray ID: {diag.get('ray_id', 'unknown')}")
        print(f"[!] Cloudflare reason: {diag.get('reason', 'unknown')}")
        print(f"[!] Container public IP (unproxied): {public_ip}")
        print(f"[!] Browser-visible public IP (proxied egress): {browser_ip}")
        if IPTVV_PROXY_URL and browser_ip not in ("unknown", "") and browser_ip == public_ip:
            print("[!] Browser IP equals container IP despite IPTVV_PROXY_URL being set "
                  "-> residential proxy is NOT taking effect.")
        elif not IPTVV_PROXY_URL:
            print("[!] No IPTVV_PROXY_URL set -> browser is on the datacenter IP (no proxy).")
        return CloudflareBlockedError(
            f"{prefix}{exc} "
            f"Container IP: {public_ip}; browser IP: {browser_ip}; "
            f"Cloudflare Ray ID: {diag.get('ray_id', 'unknown')}; {artifact_text}. "
            "Ask IPTVV/Cloudflare to allowlist this server or provide an approved API/integration path."
        )

    location = ""
    if diag.get("url") or diag.get("title"):
        location = f" Current URL: {diag.get('url')}; title: {diag.get('title')};"
    return RuntimeError(f"{prefix}{exc}{location} {artifact_text}".strip())


def preflight_checkout_access():
    """Check whether the current prod egress can reach the real IPTVV checkout."""
    driver = None
    checkout_url = f"{IPTVV_BASE_URL}/checkout/"

    print("\n" + "=" * 60)
    print("IPTVV CANADA - CHECKOUT PREFLIGHT")
    print("=" * 60)
    print(f"[*] Checkout URL: {checkout_url}")
    print(f"[*] Debug directory: {IPTVV_DEBUG_DIR}")
    print("=" * 60 + "\n")

    try:
        server_ip = get_public_ip()
        print(f"[*] Server public IP: {server_ip}")

        driver = get_driver()
        browser_ip = get_browser_public_ip(driver)
        print(f"[*] Browser-visible public IP: {browser_ip}")
        if IPTVV_KNOWN_BLOCKED_IP and browser_ip == IPTVV_KNOWN_BLOCKED_IP:
            print(f"[!] Browser is still using known blocked IPTVV IP: {IPTVV_KNOWN_BLOCKED_IP}")
        if server_ip != "unknown" and browser_ip != "unknown" and server_ip == browser_ip:
            print("[*] Browser IP matches server IP; this is expected for full-system VPN egress.")

        try:
            result = driver.preflight()
        except WorkerError as exc:
            raise _translate_worker_error(driver, exc) from exc

        artifacts = (result or {}).get("artifacts") or {}
        print("[OK] Preflight passed: IPTVV checkout form is reachable.")
        print(f"[OK] Server IP: {server_ip}")
        print(f"[OK] Browser-visible IP: {browser_ip}")
        print(f"[OK] Debug HTML: {artifacts.get('html', 'not saved')}")
        print(f"[OK] Screenshot: {artifacts.get('screenshot', 'not saved')}")
        return True

    finally:
        if driver:
            driver.close()
            print("[*] Browser closed")


def generate_random_user_data():
    """Generate random user data for checkout form."""
    first_names = ["John", "Jane", "Mike", "Sarah", "David", "Emma", "Chris", "Lisa", "Tom", "Amy"]
    last_names = ["Smith", "Johnson", "Brown", "Davis", "Wilson", "Moore", "Taylor", "Anderson"]

    first = random.choice(first_names)
    last = random.choice(last_names)

    # Generate valid Canadian phone number (area codes: 416, 514, 604, 403, 613)
    area_codes = ["416", "514", "604", "403", "613", "647", "438", "778", "587", "343"]
    area_code = random.choice(area_codes)
    exchange = f"{random.randint(200, 999)}"  # Central office code
    line = f"{random.randint(1000, 9999)}"    # Line number
    phone = f"({area_code}) {exchange}-{line}"  # Format: (416) 555-1234

    return {
        "first_name": first,
        "last_name": last,
        "phone": phone,
        "address": f"{random.randint(100, 9999)} {random.choice(['Main', 'Oak', 'Maple', 'Cedar'])} St",
        "city": random.choice(["Toronto", "Montreal", "Vancouver", "Calgary", "Ottawa"]),
        "postal_code": f"{random.choice('ABCEGHJKLMNPRSTVXY')}{random.randint(0, 9)}{random.choice('ABCEGHJKLMNPRSTVWXYZ')} {random.randint(0, 9)}{random.choice('ABCEGHJKLMNPRSTVWXYZ')}{random.randint(0, 9)}",
        "country": "Canada",
    }


# ═══════════════════════════════════════════════════════════
# IPTVV.ca Automation Functions (executed by browser/checkout.js)
# ═══════════════════════════════════════════════════════════

def navigate_to_cart_and_get_free_trial(driver):
    """Add the free-trial product to the cart, open checkout and pick the full package.

    Waits out Cloudflare challenges; raises CloudflareBlockedError / RuntimeError
    when the checkout form never loads.
    """
    try:
        driver.open_trial_checkout()
    except WorkerError as exc:
        raise _translate_worker_error(driver, exc) from exc


def fill_checkout_form(driver, email_address, names=None):
    """Fill the WooCommerce checkout form with generated data and the given email.

    'names' ({first_name, last_name}) overrides the random billing name.
    """
    user_data = generate_random_user_data()
    user_data.update(names or {})
    user_data["email"] = email_address
    try:
        driver.fill(email_address, user_data)
    except WorkerError as exc:
        raise _translate_worker_error(driver, exc) from exc
    return user_data


def _solve_with_2captcha(info):
    """Return a token for the detected widget from 2captcha (raises on failure)."""
    kind = info.get("type")
    sitekey, url = info.get("sitekey"), info.get("url")
    if kind == "turnstile":
        extra = {k: v for k, v in (("action", info.get("action")), ("data", info.get("cdata"))) if v}
        return solver.turnstile(sitekey=sitekey, url=url, **extra)["code"]
    if kind == "hcaptcha":
        return solver.hcaptcha(sitekey=sitekey, url=url)["code"]
    return solver.recaptcha(sitekey=sitekey, url=url, version="v2", invisible=0,
                            enterprise=1 if info.get("enterprise") else 0)["code"]


def solve_security_check(driver, max_retries=2):
    """Pass the checkout security check (Turnstile / reCAPTCHA / hCaptcha), if any.

    Turnstile is first given a chance to pass inside the stealth browser; 2captcha
    is used when it doesn't (and for reCAPTCHA / hCaptcha).

    Returns:
        (ok, token, captcha_type): token is None when there is no widget, it was
        passed in-browser, or it was solved by hand.
    """
    info = driver.security_check()
    if not info:
        return True, None, None
    kind = info.get("type")
    if info.get("solved"):
        return True, None, kind

    if not info.get("sitekey"):
        print(f"[!] {kind} detected but its site key could not be read; submitting without a token")
        return True, None, kind

    if not solver:
        print(f"[!] {kind} detected but TWOCAPTCHA_API_KEY not set")
        if os.getenv("HEADLESS", "True").lower() != "true":
            input("[*] Please solve the security check manually in the browser, then press ENTER here...")
            return True, None, kind
        return False, None, kind

    print(f"[*] Solving {kind} with 2captcha (site key: {info['sitekey']})")
    for attempt in range(1, max_retries + 1):
        try:
            token = _solve_with_2captcha(info)
            print(f"[OK] {kind} solved by 2captcha")
            return True, token, kind
        except Exception as exc:
            print(f"[!] {kind} attempt {attempt}/{max_retries} failed: {exc}")
            if attempt < max_retries:
                time.sleep(5)
    return False, None, kind


def submit_checkout_form(driver, security_check_retries=1):
    """Pass the security check and submit the WooCommerce checkout form.

    Re-runs the security check and resubmits when IPTVV says it wasn't completed
    (Turnstile tokens are single-use and the widget resets after a failed submit).
    """
    for attempt in range(security_check_retries + 1):
        ok, token, captcha_type = solve_security_check(driver)
        if not ok:
            raise RuntimeError(f"Failed to solve the checkout security check ({captcha_type})")

        try:
            result = driver.submit(token, captcha_type) or {}
        except WorkerError as exc:
            if exc.type == "submit_not_found":
                raise RuntimeError("Could not find or click submit button") from exc
            raise _translate_worker_error(driver, exc) from exc

        outcome = result.get("outcome")
        if outcome == "disposable_rejected":
            raise DisposableEmailRejectedError(
                "IPTVV rejected the email address as temporary/disposable; "
                "use the 'gmail' backend (a permanent address)."
            )
        if outcome != "still_checkout":
            return result

        errors = "; ".join(result.get("errors") or []) or "no WooCommerce error message found"
        print(f"[!] Checkout validation error(s): {errors}")
        if result.get("security_check_failed") and attempt < security_check_retries:
            print("[*] Security check was not accepted; re-solving and resubmitting...")
            continue
        artifacts = result.get("artifacts") or {}
        raise RuntimeError(
            f"IPTVV checkout was not accepted: {errors}. "
            f"debug HTML: {artifacts.get('html', 'not saved')}; "
            f"screenshot: {artifacts.get('screenshot', 'not saved')}"
        )
    return result



# ═══════════════════════════════════════════════════════════
# Credential Extraction
# ═══════════════════════════════════════════════════════════

def extract_credentials_from_email(message):
    """
    Extract username, password, and hostname from mail.tm message.

    Args:
        message: Full message object from mail.tm API with 'text' and 'html' fields

    Returns:
        tuple: (username, password, hostname) or (None, None, None) if extraction fails
    """
    # Get both text and HTML versions
    text_content = message.get("text", "")
    html_content = message.get("html", [])

    # Combine for comprehensive search
    if isinstance(html_content, list):
        html_content = " ".join(html_content)

    combined_content = f"{text_content}\n\n{html_content}"

    # Unescape HTML entities
    for _ in range(3):
        unescaped = html.unescape(combined_content)
        if unescaped == combined_content:
            break
        combined_content = unescaped

    # Clean HTML tags
    normalized = re.sub(r"<br\s*/?>", "\n", combined_content, flags=re.I)
    normalized = re.sub(r"</p\s*>", "\n", normalized, flags=re.I)
    normalized = re.sub(r"<[^>]+>", " ", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)

    print("[*] Extracting credentials from email...")
    print("[*] Email preview:")
    print(normalized[:500])

    # Extraction patterns based on the example email format
    username = None
    password = None
    hostname = None

    # Username patterns
    username_patterns = [
        r"Username\s*:?\s*([A-Z0-9]{10,})",  # GABSSZY5RS format
        r"Username\s*:?\s*([^\s\n<]+)",
        r"User\s*:?\s*([^\s\n<]+)",
    ]

    for pattern in username_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            username = match.group(1).strip()
            print(f"[*] Found username: {username}")
            break

    # Password patterns
    password_patterns = [
        r"Password\s*:?\s*(\d{8,})",  # 49180341 format
        r"Password\s*:?\s*([^\s\n<]+)",
        r"Pass\s*:?\s*([^\s\n<]+)",
    ]

    for pattern in password_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            password = match.group(1).strip()
            print(f"[*] Found password: {password}")
            break

    # Hostname/Server Address patterns
    hostname_patterns = [
        r"Server Address[^:]*:?\s*(https?://[^\s<>'\"]+)",
        r"Playlist Host[^:]*:?\s*(https?://[^\s<>'\"]+)",
        r"Host[^:]*:?\s*(https?://[^\s<>'\"]+)",
        r"Server[^:]*:?\s*(https?://[^\s<>'\"]+)",
    ]

    for pattern in hostname_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            hostname = match.group(1).strip().rstrip(".,)")
            print(f"[*] Found hostname: {hostname}")
            break

    # Fallback: find any URL that's not iptvv.ca or common services
    if not hostname:
        urls = re.findall(r"https?://[^\s<>'\"]+", normalized)
        for url in urls:
            lowered = url.lower()
            if "iptvv.ca" not in lowered and "mail.tm" not in lowered:
                hostname = url.rstrip(".,)")
                print(f"[*] Found hostname (fallback): {hostname}")
                break

    if not username or not password or not hostname:
        print(f"[!] Incomplete credentials (username={bool(username)}, "
              f"password={bool(password)}, hostname={bool(hostname)}). Full email body:")
        print(re.sub(r"\n\s*\n+", "\n", normalized).strip()[:3000])

    return username, password, hostname


def save_to_iboplayer(username, password, hostname, max_retries=3):
    """
    Save IPTVV playlist to IBO Player using their API.

    Args:
        username: IPTVV username
        password: IPTVV password
        hostname: IPTVV server hostname/URL
        max_retries: Maximum number of retry attempts (default: 3)

    Returns:
        bool: True if successful, False otherwise
    """
    if not IPTVV_IBOPLAYER_ENABLED:
        print("[*] IBO Player integration is disabled (IPTVV_IBOPLAYER_ENABLED=False)")
        return False

    if not IPTVV_IBOPLAYER_MAC_ADDRESS or not IPTVV_IBOPLAYER_DEVICE_KEY \
            or not TWOCAPTCHA_API_KEY or not IPTVV_IBOPLAYER_PLAYLIST_URL_ID:
        print("[!] IBO Player integration enabled but missing required credentials:")
        print(f"    - IPTVV_IBOPLAYER_MAC_ADDRESS: {'Set' if IPTVV_IBOPLAYER_MAC_ADDRESS else 'Missing'}")
        print(f"    - IPTVV_IBOPLAYER_DEVICE_KEY: {'Set' if IPTVV_IBOPLAYER_DEVICE_KEY else 'Missing'}")
        print(f"    - TWOCAPTCHA_API_KEY: {'Set' if TWOCAPTCHA_API_KEY else 'Missing'}")
        print(f"    - IPTVV_IBOPLAYER_PLAYLIST_URL_ID: {'Set' if IPTVV_IBOPLAYER_PLAYLIST_URL_ID else 'Missing'}")
        return False

    api_url = "https://iboplayer.com/frontend/device/savePlaylist"

    # Authenticate with a device-login bearer token (obtained/cached by
    # iboplayer_auth). A 401/403 below triggers a token refresh + one retry.
    try:
        headers = iboplayer_auth.authed_headers(
            IPTVV_IBOPLAYER_MAC_ADDRESS, IPTVV_IBOPLAYER_DEVICE_KEY
        )
    except Exception as e:
        print(f"[!] Could not obtain IBO Player bearer token: {e}")
        return False

    # Construct the playlist URL using the hostname from credentials
    playlist_url = hostname.rstrip("/")

    payload = {
        "current_playlist_url_id": IPTVV_IBOPLAYER_PLAYLIST_URL_ID,
        "password": password,
        "pin": "",
        "playlist_name": IPTVV_IBOPLAYER_PLAYLIST_NAME,
        "playlist_type": "xc",  # Xtream Codes format
        "playlist_url": playlist_url,
        "protect": "false",
        "username": username,
        "xml_url": ""
    }

    print("\n" + "=" * 60)
    print("[*] Saving playlist to IBO Player...")
    print("=" * 60)
    print(f"[*] API URL: {api_url}")
    print(f"[*] Playlist Name: {IPTVV_IBOPLAYER_PLAYLIST_NAME}")
    print(f"[*] Playlist URL: {playlist_url}")
    print(f"[*] Username: {username}")
    print(f"[*] Password: {password}")
    print("=" * 60)

    relogin_attempted = False
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                api_url,
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                # HTTP 200 alone is NOT success: IBO Player returns
                # {"status":"error"} in the body when it rejects the save
                # (e.g. the current_playlist_url_id isn't owned by this device).
                try:
                    response_data = response.json()
                except Exception:
                    response_data = None
                print(f"[*] IBO Player response: {response_data if response_data is not None else response.text[:300]}")

                if isinstance(response_data, dict) and response_data.get("status") == "success":
                    print(f"[OK] Playlist saved to IBO Player successfully!")
                    return True

                print("[!] IBO Player rejected the save (status != success).")
                print("[!] Most common cause: IPTVV_IBOPLAYER_PLAYLIST_URL_ID is not a "
                      "playlist owned by the logged-in device "
                      f"({IPTVV_IBOPLAYER_MAC_ADDRESS}). Verify the device MAC/key and "
                      "playlist URL ID match the same IBO Player device.")
                return False

            elif response.status_code in (401, 403):
                # Expired/invalid bearer token - refresh it once and retry.
                print(f"[!] IBO Player auth rejected ({response.status_code}): {response.text[:200]}")
                if not relogin_attempted:
                    relogin_attempted = True
                    print("[*] Refreshing bearer token and retrying...")
                    iboplayer_auth.clear_cached_token(IPTVV_IBOPLAYER_MAC_ADDRESS)
                    try:
                        headers = iboplayer_auth.authed_headers(
                            IPTVV_IBOPLAYER_MAC_ADDRESS, IPTVV_IBOPLAYER_DEVICE_KEY,
                            force_refresh=True,
                        )
                    except Exception as e:
                        print(f"[!] Re-login failed: {e}")
                        return False
                    continue
                print("[!] Re-login already attempted - giving up.")
                return False

            elif 400 <= response.status_code < 500:
                # Client error - don't retry, configuration issue
                print(f"[!] IBO Player API error {response.status_code}: {response.text[:200]}")
                print(f"[!] This is a configuration error - please check your IBO Player credentials")
                return False

            else:
                # Server error - retry with exponential backoff
                print(f"[!] IBO Player API error {response.status_code} (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff: 2s, 4s, 8s
                    print(f"[*] Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)

        except requests.exceptions.Timeout:
            print(f"[!] IBO Player API timeout (attempt {attempt}/{max_retries})")
            if attempt < max_retries:
                wait_time = 2 ** attempt
                print(f"[*] Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

        except Exception as e:
            print(f"[!] IBO Player API exception (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                wait_time = 2 ** attempt
                print(f"[*] Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

    print(f"[!] Failed to save playlist to IBO Player after {max_retries} attempts")
    return False


def update_github_playlist(m3u_url, max_retries=3):
    """
    Store the M3U URL in the playlist-updater repo's Actions secret and
    dispatch the workflow that rebuilds the playlist from it.

    Args:
        m3u_url: Full get.php M3U URL for the new trial
        max_retries: Maximum attempts per request on 5xx/timeouts (default: 3)

    Returns:
        bool: True if the secret was updated and the workflow dispatched
    """
    if not IPTVV_GITHUB_ENABLED:
        print("[*] GitHub playlist update is disabled (IPTVV_GITHUB_ENABLED=False)")
        return False

    if not IPTVV_GITHUB_TOKEN:
        print("[!] GitHub playlist update enabled but IPTVV_GITHUB_TOKEN is missing")
        return False

    try:
        from nacl import encoding, public
    except ImportError:
        print("[!] PyNaCl is not installed - cannot encrypt the GitHub secret")
        return False

    api = f"https://api.github.com/repos/{IPTVV_GITHUB_REPO}"
    headers = {
        "Authorization": f"Bearer {IPTVV_GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def request(method, url, **kwargs):
        """Send one GitHub API call, retrying 5xx/timeouts. Returns the response or None."""
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
                if response.status_code < 400:
                    return response
                if response.status_code < 500:
                    # Client error - don't retry, token/repo configuration issue
                    print(f"[!] GitHub API error {response.status_code} on {method} {url}: {response.text[:200]}")
                    print("[!] Check the token has Secrets + Actions write access to the repo")
                    return None
                print(f"[!] GitHub API error {response.status_code} (attempt {attempt}/{max_retries})")
            except requests.RequestException as e:
                print(f"[!] GitHub API request failed (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        return None

    print("\n" + "=" * 60)
    print(f"[*] Updating GitHub secret {IPTVV_GITHUB_SECRET_NAME} in {IPTVV_GITHUB_REPO}...")
    print("=" * 60)

    try:
        # 1) The secret must be sealed with the repo's Actions public key
        key_resp = request("GET", f"{api}/actions/secrets/public-key")
        if key_resp is None:
            return False
        key = key_resp.json()
        pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
        encrypted = base64.b64encode(public.SealedBox(pk).encrypt(m3u_url.encode())).decode()

        if request("PUT", f"{api}/actions/secrets/{IPTVV_GITHUB_SECRET_NAME}",
                   json={"encrypted_value": encrypted, "key_id": key["key_id"]}) is None:
            return False
        print(f"[OK] Secret {IPTVV_GITHUB_SECRET_NAME} updated")

        # 2) Trigger the workflow so the playlist picks up the new source now
        if request("POST", f"{api}/actions/workflows/{IPTVV_GITHUB_WORKFLOW}/dispatches",
                   json={"ref": IPTVV_GITHUB_REF}) is None:
            return False
        print(f"[OK] Workflow {IPTVV_GITHUB_WORKFLOW} dispatched on {IPTVV_GITHUB_REF}")
        return True

    except Exception as e:
        print(f"[!] GitHub playlist update failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════
# Webhook & Notification Integration
# ═══════════════════════════════════════════════════════════

def send_webhook_callback(callback_url, user_id, status, username=None, password=None, host=None, m3u_url=None, error=None, max_retries=3):
    """Send webhook callback to Laravel backend."""
    if not callback_url:
        print("[*] No callback URL provided, skipping webhook")
        return False

    payload = {
        "user_id": user_id,
        "status": status,
        "timestamp": datetime.now().isoformat(),
    }
    if status == "success":
        payload.update({
            "username": username,
            "password": password,
            "host": host,
            "m3u_url": m3u_url
        })
    else:
        payload["error"] = error

    headers = {"Content-Type": "application/json", "User-Agent": "IPTVV-Canada-Automation/1.0"}
    webhook_token = os.getenv("WEBHOOK_AUTH_TOKEN", "")
    if webhook_token:
        headers["Authorization"] = f"Bearer {webhook_token}"

    print(f"[*] Sending webhook: {json.dumps({**payload, 'password': '***' if password else None}, indent=2)}")
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(callback_url, json=payload, headers=headers, timeout=30)
            print(f"[*] Webhook response status: {response.status_code}")
            if response.status_code in (200, 201, 202):
                print("[OK] Webhook sent successfully")
                return True
            if 400 <= response.status_code < 500:
                print(f"[!] Webhook client error: {response.text[:500]}")
                return False
        except requests.RequestException as exc:
            print(f"[!] Webhook request failed: {exc}")
        if attempt < max_retries:
            time.sleep(2 ** attempt)
    return False


def send_telegram_notification(status, message, details=None):
    """Send Telegram notification using the notifier module."""
    try:
        if status == "success":
            notifier.notify_success(message, details, None)
        else:
            notifier.notify_error(message, details, None)
    except Exception as exc:
        print(f"[!] Telegram notification failed: {exc}")


# ═══════════════════════════════════════════════════════════
# Main Automation Flow
# ═══════════════════════════════════════════════════════════

def run_automation():
    """Execute the full trial-creation flow and return the extracted credentials.

    Runs the WooCommerce checkout, waits for the credentials email, extracts the
    Xtream credentials and (if enabled) saves them to IBO Player. Raises on any
    failure. The caller owns webhook/Telegram notifications and result reporting,
    so this function is reusable by both the CLI and the Apify Actor wrapper.

    Returns:
        dict: {email, username, password, host, m3u_url}
    """
    driver = None
    try:
        print("\n[*] Creating trial using public IP (direct connection)")
        print("=" * 60)

        # Drop backends with broken credentials (bad Gmail app password,
        # over-quota mail.cx token, ...) before spending a browser session
        # (checkout + captcha) that a broken inbox would waste.
        email_backends = [b for b in email_backend_chain() if email_backend_ready(b)]
        if not email_backends:
            raise RuntimeError(f"No usable email backend (tried {', '.join(email_backend_chain())})")
        print(f"[*] Email backends in order: {', '.join(email_backends)}")

        # Step 1: Initialize browser and confirm IPTVV checkout is reachable.
        driver = get_driver()

        # Steps 2-3: Add the trial to the cart, open checkout (waiting out any
        # Cloudflare challenge) and select the full channel package.
        navigate_to_cart_and_get_free_trial(driver)

        # Steps 4-6: allocate an address, fill and submit checkout. If IPTVV
        # rejects the address as disposable, retry on the same checkout page
        # with a fresh address on another domain (or the next backend).
        for email_attempt in range(1, IPTVV_EMAIL_REJECT_RETRIES + 1):
            # Step 4: Allocate a receiving address only after the checkout form is reachable.
            email_session = create_email_session(email_backends)
            if not email_session:
                raise RuntimeError(f"Failed to create temporary email (tried {', '.join(email_backends)})")
            email_address = email_session["address"]

            # Step 5: Fill checkout form with the temporary email
            fill_checkout_form(driver, email_address, email_session.get("names"))

            # Step 6: Submit form
            try:
                submit_checkout_form(driver)
                break
            except DisposableEmailRejectedError:
                domain = email_address.rsplit("@", 1)[-1].lower()
                backend = email_session["backend"]
                hint = "; add it to BOOMLIFY_BLOCKED_DOMAINS" if backend == "boomlify" else ""
                if backend == "boomlify":
                    _boomlify_rejected_domains.add(domain)
                print(f"[!] {domain} ({backend}) rejected as disposable (attempt {email_attempt}/"
                      f"{IPTVV_EMAIL_REJECT_RETRIES}){hint}")
                if email_attempt == IPTVV_EMAIL_REJECT_RETRIES:
                    raise
                # Fall through to the next backend (e.g. gmail) instead of
                # burning attempts on more addresses from a rejected one.
                remaining = [b for b in email_backends if b != backend]
                if remaining:
                    email_backends = remaining
                    print(f"[*] Dropping {backend}; retrying checkout with {email_backends[0]}...")
                else:
                    print(f"[*] No other email backend configured; retrying {backend} with another address...")

        # Step 7: Wait for credentials email (this can take 5-45 minutes)
        print("\n" + "=" * 60)
        print(f"[*] Order submitted! Monitoring {email_session['backend']} inbox: {email_address}")
        print("=" * 60 + "\n")

        credentials_message = wait_for_credentials_email(driver, email_session)
        if not credentials_message:
            raise RuntimeError(f"Timeout: Credentials email not received after {EMAIL_MAX_WAIT_SECONDS} seconds")

        # Step 8: Extract credentials from email
        username, password, hostname = extract_credentials_from_email(credentials_message)

        if not username or not password or not hostname:
            raise RuntimeError("Failed to extract complete credentials from email")

        # Construct M3U URL
        m3u_url = f"{hostname.rstrip('/')}/get.php?username={username}&password={password}&type=m3u_plus&output=ts"

        # Success!
        print("\n" + "=" * 60)
        print("\u2713 IPTVV CANADA CREDENTIALS EXTRACTED SUCCESSFULLY")
        print("=" * 60)
        print(f"[*] Server Address: {hostname}")
        print(f"[*] Username: {username}")
        print(f"[*] Password: {password}")
        print(f"[*] M3U URL: {m3u_url}")
        print("=" * 60 + "\n")

        # Save to IBO Player if enabled
        save_to_iboplayer(username, password, hostname)

        # Update the playlist on the IPTV player if configured
        player_updated = (
            player_api.update_player_playlist(hostname, username, password)
            if player_api.player_enabled() else False
        )

        # Push the M3U URL to the GitHub playlist-updater if enabled
        github_updated = update_github_playlist(m3u_url)

        return {
            "email": email_address,
            "username": username,
            "password": password,
            "host": hostname,
            "m3u_url": m3u_url,
            "github_updated": github_updated,
            "player_updated": player_updated,
        }

    finally:
        if driver and AUTO_EXIT:
            try:
                driver.close()
                print("[*] Browser closed")
            except Exception:
                pass
        elif driver:
            print("[*] AUTO_EXIT disabled; browser will remain open")
            while True:
                time.sleep(1)


def main(user_id=None, callback_url=None):
    """Main automation workflow (CLI entry point)."""
    is_laravel_mode = bool(user_id and callback_url)

    print("\n" + "=" * 60)
    print("IPTVV CANADA - AUTOMATED TRIAL CREATION")
    print("=" * 60)
    print(f"[*] User ID: {user_id if user_id else 'N/A'}")
    print(f"[*] Callback URL: {callback_url if callback_url else 'N/A'}")
    print(f"[*] Laravel integration mode: {is_laravel_mode}")
    print("=" * 60 + "\n")

    try:
        result = run_automation()

        # Send success notifications
        if is_laravel_mode:
            send_webhook_callback(
                callback_url=callback_url,
                user_id=user_id,
                status="success",
                username=result["username"],
                password=result["password"],
                host=result["host"],
                m3u_url=result["m3u_url"],
            )

        send_telegram_notification(
            "success",
            f"IPTVV Canada trial created for {result['email']}",
            f"Username: {result['username']}\nHost: {result['host']}"
        )

        print("[OK] IPTVV Canada automation complete")

    except (CloudflareBlockedError, TrialRejectedError) as exc:
        # No proxy fallback configured - report on the public IP result and exit.
        print(f"\n[!] IPTVV Canada automation failed: {exc}")

        if is_laravel_mode:
            send_webhook_callback(
                callback_url=callback_url,
                user_id=user_id,
                status="failed",
                error=str(exc)
            )
        send_telegram_notification("error", type(exc).__name__, str(exc))
        raise SystemExit(1)

    except Exception as exc:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"\n[!] IPTVV Canada automation failed: {exc}")
        print(error_traceback)

        if is_laravel_mode:
            send_webhook_callback(
                callback_url=callback_url,
                user_id=user_id,
                status="failed",
                error=f"{exc}\n\n{error_traceback}"
            )

        send_telegram_notification("error", str(exc), error_traceback)
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="IPTVV Canada - Automated Trial Account Creation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python iptvvcanada_automation.py
  python iptvvcanada_automation.py --preflight-only
  python iptvvcanada_automation.py --user-id 123 --callback-url https://app.com/api/webhooks/iptvv-automation
        """,
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Only verify IPTVV checkout reachability and browser egress; do not create a trial",
    )
    parser.add_argument("--user-id", type=int, help="Laravel IPTV account ID")
    parser.add_argument("--callback-url", type=str, help="Webhook callback URL")
    args = parser.parse_args()

    if args.preflight_only:
        try:
            preflight_checkout_access()
        except Exception as exc:
            import traceback
            error_traceback = traceback.format_exc()
            print(f"\n[!] IPTVV Canada preflight failed: {exc}")
            print(error_traceback)
            raise SystemExit(1)
    else:
        main(user_id=args.user_id, callback_url=args.callback_url)

#!/usr/bin/env python3
"""Independent Granola login; never refresh or modify desktop-owned credentials."""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import plistlib
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

AUTH_FILE = Path(os.environ.get(
    "GRANOLA_AUTH_FILE",
    str(Path.home() / "Library/Application Support/granola-to-drive/auth.json"),
))
DESKTOP_FILE = Path.home() / "Library/Application Support/Granola/supabase.json"
API_BASE = "https://api.granola.ai"


class AuthError(RuntimeError):
    pass


def client_version():
    override = os.environ.get("GRANOLA_CLIENT_VERSION")
    if override:
        return override
    try:
        with open("/Applications/Granola.app/Contents/Info.plist", "rb") as f:
            return plistlib.load(f)["CFBundleShortVersionString"]
    except (OSError, KeyError, plistlib.InvalidFileException):
        return "7.609.0"


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def auth_lock():
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(AUTH_FILE) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def token_expiry(tokens):
    # Prefer the actual token expiry; obtained_at varied across desktop versions.
    try:
        payload = tokens["access_token"].split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])
    except (KeyError, IndexError, ValueError, TypeError):
        obtained = float(tokens.get("obtained_at", 0))
        if obtained > 1e11:
            obtained /= 1000
        return obtained + float(tokens.get("expires_in", 0))


def auth_post(endpoint, body, access_token=None):
    headers = {"Content-Type": "application/json", "X-Client-Version": client_version(),
               "User-Agent": "granola-to-drive"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    req = urllib.request.Request(API_BASE + endpoint, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Auth response bodies may contain credential material. Never log them.
        raise AuthError(f"Granola authentication returned HTTP {exc.code}. Run granola_auth.py login again.") from None
    except urllib.error.URLError as exc:
        raise AuthError(f"Cannot reach Granola authentication: {exc.reason}") from None


def normalize_tokens(response):
    tokens = response.get("tokens", response.get("workos_tokens", response))
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    if not isinstance(tokens, dict) or not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise AuthError("Granola returned an unexpected authentication response (credentials not saved).")
    tokens = dict(tokens)
    tokens.setdefault("obtained_at", int(time.time() * 1000))
    return tokens


def load_token():
    explicit = os.environ.get("GRANOLA_ACCESS_TOKEN")
    if explicit:
        return explicit
    with auth_lock():
        if AUTH_FILE.exists():
            try:
                tokens = json.loads(AUTH_FILE.read_text())
            except (OSError, ValueError) as exc:
                raise AuthError("Cannot read sync credentials. Run granola_auth.py login again.") from exc
            if token_expiry(tokens) < time.time() + 300:
                if not tokens.get("refresh_token"):
                    raise AuthError("Sync credentials have no refresh token. Run granola_auth.py login again.")
                response = auth_post("/v1/refresh-access-token",
                                     {"refresh_token": tokens["refresh_token"]}, tokens["access_token"])
                tokens = normalize_tokens(response)
                # Persist the rotated refresh token before using the access token.
                atomic_json(AUTH_FILE, tokens)
                print("Renewed sync sign-in.")
            return tokens["access_token"]

    if DESKTOP_FILE.with_suffix(".json.enc").exists():
        raise AuthError("Granola now encrypts its desktop credentials. Run granola_auth.py login once to connect this sync.")
    if DESKTOP_FILE.exists():
        try:
            tokens = json.loads(DESKTOP_FILE.read_text())["workos_tokens"]
            tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            if token_expiry(tokens) > time.time() + 60:
                return tokens["access_token"]
        except (ValueError, KeyError, TypeError):
            pass
    raise AuthError("No current Granola sign-in. Run granola_auth.py login to connect this sync.")


def begin_login(provider="google"):
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    click_id = str(uuid.uuid4())
    atomic_json(AUTH_FILE.with_suffix(".pending.json"), {
        "verifier": verifier, "sign_in_click_id": click_id, "created_at": time.time(),
    })
    query = urllib.parse.urlencode({
        "dev": "false", "code_challenge": challenge, "platform": "macOS",
        "version": client_version(), "sign_in_click_id": click_id,
        "intent": "download", "provider": provider, "request_calendar_permissions": "false",
    })
    return API_BASE + "/v1/auth?" + query


def complete_login(callback):
    url = urllib.parse.urlparse(callback.strip())
    desktop_callback = url.scheme == "granola" and url.netloc == "login-complete"
    web_callback = (url.scheme == "https" and url.netloc in {"www.granola.ai", "granola.ai"}
                    and url.path == "/app-redirect")
    if not (desktop_callback or web_callback):
        raise AuthError("Expected the Granola app-redirect URL or granola://login-complete link.")
    params = urllib.parse.parse_qs(url.query)
    code = params.get("code", [None])[0]
    if not code:
        raise AuthError("The callback link has no authorization code.")
    with auth_lock():
        pending_path = AUTH_FILE.with_suffix(".pending.json")
        try:
            pending = json.loads(pending_path.read_text())
        except (OSError, ValueError):
            raise AuthError("No pending login. Run granola_auth.py login first.") from None
        if time.time() - pending["created_at"] > 1800:
            raise AuthError("Login has expired. Run granola_auth.py login again.")
        click_id = params.get("signInClickId", [pending["sign_in_click_id"]])[0]
        if click_id != pending["sign_in_click_id"]:
            raise AuthError("This callback belongs to a different login attempt.")
        response = auth_post("/v1/workos-auth-complete", {
            "code": code, "codeVerifier": pending["verifier"], "platform": "macOS",
            "isDev": False, "sso": params.get("sso", ["false"])[0] == "true",
            "ssoCode": params.get("ssoCode", [""])[0], "signInClickId": click_id,
            "authContext": params.get("authContext", [None])[0],
        })
        atomic_json(AUTH_FILE, normalize_tokens(response))
        pending_path.unlink()
    print("Granola sync connected. Credentials will renew automatically.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["login", "complete", "status"])
    parser.add_argument("--provider", choices=["google", "microsoft"], default="google")
    args = parser.parse_args()
    try:
        if args.command == "login":
            print("Open this URL in your browser:\n" + begin_login(args.provider))
            print("After sign-in, cancel the app-open prompt and copy the browser address.")
            print("Run: python3 granola_auth.py complete — then paste the link at its prompt.")
        elif args.command == "complete":
            import getpass
            callback = getpass.getpass("Open Granola link (hidden): ") if sys.stdin.isatty() else sys.stdin.readline()
            complete_login(callback)
        else:
            load_token()
            print("Granola sign-in OK.")
    except (AuthError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

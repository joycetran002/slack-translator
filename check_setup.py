"""
check_setup.py
==============

Pre-flight validator. Run this before `python app.py` to confirm your tokens,
scopes, and API key are all good — it turns the usual "stack trace on the third
line of app.py" into a clear pass/fail checklist.

    python check_setup.py

Exits 0 if everything required passes, 1 otherwise. Scope gaps are reported as
warnings (the app may still partly work) unless a token is outright invalid.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ANSI helpers (fall back to plain text if not a TTY)
_TTY = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _TTY else s
OK = _c("32", "PASS")
BAD = _c("31", "FAIL")
WARN = _c("33", "WARN")

errors = 0
warnings = 0


def fail(msg: str):
    global errors
    errors += 1
    print(f"  [{BAD}] {msg}")


def warn(msg: str):
    global warnings
    warnings += 1
    print(f"  [{WARN}] {msg}")


def ok(msg: str):
    print(f"  [{OK}] {msg}")


# Required bot/user scopes (mirrors README). Used to flag missing scopes.
BOT_SCOPES = {
    "channels:history", "groups:history", "im:history", "mpim:history",
    "chat:write", "commands",
}
USER_SCOPES = {
    "chat:write", "channels:history", "groups:history", "im:history", "mpim:history",
}


ENGINE = os.environ.get("TRANSLATION_ENGINE", "claude").lower()


def check_env():
    print(f"1. Environment variables (engine: {ENGINE})")
    required = ["SLACK_BOT_TOKEN", "SLACK_USER_TOKEN", "SLACK_APP_TOKEN"]
    if ENGINE == "claude":
        required.append("ANTHROPIC_API_KEY")  # only the paid backend needs it
    missing = [k for k in required if not os.environ.get(k)]
    for k in required:
        val = os.environ.get(k)
        if val:
            ok(f"{k} set ({val[:8]}…)")
        else:
            fail(f"{k} is missing from .env")
    # Prefix sanity checks (cheap, catches paste mistakes)
    prefixes = {
        "SLACK_BOT_TOKEN": "xoxb-",
        "SLACK_USER_TOKEN": "xoxp-",
        "SLACK_APP_TOKEN": "xapp-",
        "ANTHROPIC_API_KEY": "sk-ant-",
    }
    for k, pfx in prefixes.items():
        v = os.environ.get(k)
        if v and not v.startswith(pfx):
            warn(f"{k} doesn't start with '{pfx}' — did you paste the right token?")
    return not missing


def _scopes_from_response(resp) -> set[str]:
    """OAuth scopes are returned in the x-oauth-scopes response header."""
    raw = resp.headers.get("x-oauth-scopes", "") if hasattr(resp, "headers") else ""
    return {s.strip() for s in raw.split(",") if s.strip()}


def check_slack_token(label, token, expected_scopes):
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    print(f"\n{label}")
    client = WebClient(token=token)
    try:
        resp = client.auth_test()
    except SlackApiError as e:
        fail(f"auth.test failed: {e.response.get('error')}")
        return None
    ok(f"authenticated as '{resp.get('user')}' in team '{resp.get('team')}'")
    granted = _scopes_from_response(resp)
    if granted:
        missing = expected_scopes - granted
        if missing:
            warn(f"missing scopes: {', '.join(sorted(missing))} (reinstall the app after adding them)")
        else:
            ok("all required scopes present")
    else:
        warn("could not read scopes from response headers; verify manually in the app config")
    return resp


def check_app_token(token):
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    print("\n4. App-level token (Socket Mode)")
    client = WebClient()
    try:
        # This endpoint authenticates with the app-level token passed explicitly,
        # not the client's default token.
        resp = client.apps_connections_open(app_token=token)
    except SlackApiError as e:
        err = e.response.get("error")
        if err == "not_allowed_token_type":
            fail("this isn't an app-level token (need xapp- with connections:write)")
        else:
            fail(f"apps.connections.open failed: {err}")
        return
    if resp.get("url"):
        ok("Socket Mode connection opened successfully")


def check_google():
    print("\n5. Google translation engine (free)")
    try:
        from deep_translator import GoogleTranslator
        import langdetect  # noqa: F401
    except ImportError as e:
        fail(f"missing package: {e.name} — run `pip install -r requirements.txt`")
        return
    try:
        out = GoogleTranslator(source="auto", target="en").translate("hola mundo")
        if out:
            ok(f"Google endpoint reachable (test: 'hola mundo' -> {out!r})")
        else:
            warn("Google endpoint returned empty output")
    except Exception as e:
        fail(f"Google translation test failed: {e}")


def check_anthropic():
    print("\n5. Anthropic API key + model")
    import anthropic

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic()
    try:
        # models.retrieve validates the key and the model id without generating tokens.
        m = client.models.retrieve(model)
        ok(f"API key valid; model '{m.id}' is available")
    except anthropic.AuthenticationError:
        fail("ANTHROPIC_API_KEY is invalid")
    except anthropic.NotFoundError:
        fail(f"model '{model}' not found — check ANTHROPIC_MODEL")
    except anthropic.APIError as e:
        fail(f"Anthropic API error: {e}")


def main():
    print("Slack Auto-Translator — setup check\n")

    if not check_env():
        print(f"\n{BAD}: fix the missing variables in .env, then re-run.")
        sys.exit(1)

    bot = check_slack_token("2. Bot token (xoxb)", os.environ["SLACK_BOT_TOKEN"], BOT_SCOPES)
    user = check_slack_token("3. User token (xoxp) — this is 'you'", os.environ["SLACK_USER_TOKEN"], USER_SCOPES)
    check_app_token(os.environ["SLACK_APP_TOKEN"])
    if ENGINE == "google":
        check_google()
    else:
        check_anthropic()

    if user and user.get("user_id"):
        print(f"\nOutgoing messages will be sent as: {user.get('user')} ({user['user_id']})")

    print("\n" + "-" * 50)
    if errors:
        print(f"{BAD}: {errors} error(s), {warnings} warning(s). Fix errors before running app.py.")
        sys.exit(1)
    elif warnings:
        print(f"{WARN}: {warnings} warning(s) but no blocking errors. You can run app.py.")
        sys.exit(0)
    else:
        print(f"{OK}: everything checks out. Run `python app.py`.")
        sys.exit(0)


if __name__ == "__main__":
    main()

"""
app.py
======

Slack auto-translation bot built on Slack Bolt (Socket Mode) + the Anthropic
Claude API.

Two directions:

  INCOMING (others -> me): when someone else posts in a watched conversation,
    detect the language; if it differs from my reading language for that
    conversation, post the translation as a threaded reply (original stays put).

  OUTGOING (me -> others): when I post, delete my original message and re-post
    the translated version *as me* (user token), so it appears in my place.
    Target = the recipients' language (auto-detected) or a forced per-conversation
    target.

Bypass: prefix a message with `!raw ` to send it untranslated (the prefix is
stripped on the outgoing path).

-----------------------------------------------------------------------------
IMPORTANT SLACK LIMITATION (read me)
-----------------------------------------------------------------------------
The modern Events API (which Socket Mode uses) delivers message events only for
conversations the *app* (its bot user) belongs to. Consequences:

  * Channels / group DMs (mpim) where the bot is a member  -> fully supported,
    both directions.
  * Your private 1:1 DMs with other people                 -> the bot cannot be
    a member of a 2-person DM, so it never receives those events. Incoming and
    outgoing translation in 1:1 DMs is therefore NOT achievable with the
    event-driven approach, and that is a platform constraint, not a bug here.

The closest workable alternative for true 1:1-DM coverage is to *poll*
`conversations.history` with the user token on a timer instead of subscribing to
events. That trades real-time delivery for coverage; see the README. This file
implements the event-driven path, which is what `slack_bolt` + Socket Mode is
designed for, and degrades cleanly (skips) when it lacks access.

Deleting your own messages and re-posting as you: both work with a user token
(xoxp) carrying `chat:write` — `chat.delete` succeeds on messages you authored,
and `chat.postMessage` with the user token posts under your identity.
"""

from __future__ import annotations

import logging
import os
import time

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from store import BotMessageTracker, CachedMessage, ConfigStore, MessageCache
from translator import ContextLine, build_translator

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_USER_TOKEN = os.environ["SLACK_USER_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]

# Translation engine + model are read by translator.build_translator() from
# TRANSLATION_ENGINE and ANTHROPIC_MODEL.

DEFAULT_READING_LANG = os.environ.get("DEFAULT_READING_LANG", "en")
DEFAULT_TARGET_LANG = os.environ.get("DEFAULT_TARGET_LANG", "en")
DB_PATH = os.environ.get("DB_PATH", "translator.db")
CONTEXT_MESSAGES = int(os.environ.get("CONTEXT_MESSAGES", "8"))
RAW_PREFIX = os.environ.get("RAW_PREFIX", "!raw")
# Post incoming translations as an ephemeral note instead of a threaded reply.
INCOMING_EPHEMERAL = os.environ.get("INCOMING_EPHEMERAL", "false").lower() == "true"

# --------------------------------------------------------------------------- #
# Singletons
# --------------------------------------------------------------------------- #

app = App(token=SLACK_BOT_TOKEN)
user_client = WebClient(token=SLACK_USER_TOKEN)  # acts AS me (delete + repost)

config = ConfigStore(DB_PATH, DEFAULT_READING_LANG, DEFAULT_TARGET_LANG)
cache = MessageCache(max_per_conversation=max(20, CONTEXT_MESSAGES * 2))
bot_msgs = BotMessageTracker()
translator = build_translator()  # claude or google, per TRANSLATION_ENGINE

# Resolved at startup: the user id behind the user token (that's "me").
MY_USER_ID: str = ""


# --------------------------------------------------------------------------- #
# Slack call helper (handles rate limiting)
# --------------------------------------------------------------------------- #


def slack_call(fn, **kwargs):
    """
    Call a slack_sdk method, retrying once on HTTP 429 using Retry-After.
    Returns the response, or None if the call ultimately fails.
    """
    for attempt in range(3):
        try:
            return fn(**kwargs)
        except SlackApiError as e:
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", "1"))
                log.warning("Slack rate limited; sleeping %ss", retry_after)
                time.sleep(retry_after)
                continue
            log.warning("Slack API error on %s: %s", fn.__name__, e.response.get("error"))
            return None
    return None


def build_context(channel_id: str) -> list[ContextLine]:
    """Recent messages for translation context (excludes the current one)."""
    return [
        ContextLine(text=m.text, is_me=m.is_me)
        for m in cache.recent(channel_id, limit=CONTEXT_MESSAGES)
    ]


_profile_cache: dict = {}


def get_profile(user_id):
    """Resolve a user's display name + avatar (cached). Needs the `users:read`
    bot scope. Returns {'name', 'icon'} or None if it can't be fetched."""
    if user_id in _profile_cache:
        return _profile_cache[user_id]
    prof = None
    resp = slack_call(app.client.users_info, user=user_id)
    if resp and resp.get("user"):
        p = resp["user"].get("profile", {}) or {}
        prof = {
            "name": p.get("display_name") or p.get("real_name") or resp["user"].get("name"),
            "icon": p.get("image_72") or p.get("image_48"),
        }
    _profile_cache[user_id] = prof
    return prof


# --------------------------------------------------------------------------- #
# Core: message event handler
# --------------------------------------------------------------------------- #


@app.event("message")
def handle_message(event, logger):
    # Ignore edits/deletes/joins and any bot-authored messages outright. We only
    # act on plain, human-authored new messages.
    if event.get("subtype") is not None:
        return
    if event.get("bot_id"):
        return

    channel_id = event.get("channel")
    ts = event.get("ts")
    user = event.get("user")
    text = event.get("text", "")

    if not channel_id or not ts or not text:
        return

    # Loop prevention: if this ts is something we posted/reposted, skip it.
    if bot_msgs.is_ours(ts):
        return

    is_me = user == MY_USER_ID
    cfg = config.get(channel_id)

    if not cfg.enabled:
        # Still cache so context/recipient-language stays warm for when re-enabled.
        cache.add(channel_id, CachedMessage(user=user, text=text, is_me=is_me))
        return

    if is_me:
        _handle_outgoing(channel_id, ts, text, event.get("thread_ts"))
    else:
        _handle_incoming(channel_id, ts, user, text, cfg.reading_lang, event.get("thread_ts"))


def _handle_incoming(channel_id, ts, user, text, reading_lang, thread_ts=None):
    """Someone else posted -> translate into MY reading language for this convo."""
    context = build_context(channel_id)

    result = translator.translate(text, target_lang=reading_lang, context=context)

    # Cache the message (with detected language if we have it) for context and
    # for recipient-language detection on the outgoing path.
    detected = result.detected_lang if result else None
    cache.add(
        channel_id,
        CachedMessage(user=user, text=text, is_me=False, detected_lang=detected),
    )

    if result is None or not result.needs_translation:
        return  # already in my reading language, or translation failed

    # Private (ephemeral) note — ONLY I can see it — shown inside the sender's
    # message thread. The sender's name is in the text since ephemerals can't carry
    # another user's avatar. Note: you must OPEN the thread to see it (Slack doesn't
    # flag a reply on the message for ephemerals).
    reply_root = thread_ts or ts
    prof = get_profile(user)
    body = f":speech_balloon: ({detected} → {reading_lang})\n{result.translated_text}"
    kwargs = {
        "channel": channel_id,
        "user": MY_USER_ID,
        "text": body,
        "thread_ts": reply_root,
    }
    if prof and prof.get("name"):
        kwargs["username"] = prof["name"]      # show the sender's name as the label
        if prof.get("icon"):
            kwargs["icon_url"] = prof["icon"]
    slack_call(app.client.chat_postEphemeral, **kwargs)


def _handle_outgoing(channel_id, ts, text, thread_ts=None):
    """
    I posted -> delete my original and re-post the translated version as me.
    Target = forced per-conversation target, else recipients' detected language,
    else the configured default.

    If the message was a threaded reply, re-post it into the same thread so the
    translation stays where the conversation is happening.
    """
    cfg = config.get(channel_id)

    # Only treat it as a thread reply when thread_ts differs from the message's own
    # ts (a thread *parent* has thread_ts == ts, and we don't want to re-parent it).
    reply_thread = thread_ts if thread_ts and thread_ts != ts else None

    # Bypass: `!raw ` sends untranslated. We still delete + repost so the prefix
    # itself doesn't appear in the channel.
    if text.startswith(RAW_PREFIX):
        raw_body = text[len(RAW_PREFIX):].lstrip()
        _replace_my_message(channel_id, ts, raw_body, reply_thread)
        cache.add(channel_id, CachedMessage(user=MY_USER_ID, text=raw_body, is_me=True))
        return

    target = cfg.target_lang or cache.recipient_language(channel_id) or DEFAULT_TARGET_LANG

    context = build_context(channel_id)
    result = translator.translate(text, target_lang=target, context=context)

    cache.add(channel_id, CachedMessage(user=MY_USER_ID, text=text, is_me=True))

    if result is None or not result.needs_translation:
        return  # already in the recipients' language, or translation failed

    new_ts = _replace_my_message(channel_id, ts, result.translated_text, reply_thread)

    # Privately remind me what I originally wrote — ephemeral, ONLY I can see it —
    # shown inside the translated message's thread. (Open the thread to see it.)
    note_thread = reply_thread or new_ts
    kwargs = {
        "channel": channel_id,
        "user": MY_USER_ID,
        "text": f":memo: Your original message: {text}",
    }
    if note_thread:
        kwargs["thread_ts"] = note_thread
    me_prof = get_profile(MY_USER_ID)
    if me_prof and me_prof.get("name"):
        kwargs["username"] = me_prof["name"]   # show my account name as the label
        if me_prof.get("icon"):
            kwargs["icon_url"] = me_prof["icon"]
    slack_call(app.client.chat_postEphemeral, **kwargs)


def _replace_my_message(channel_id, original_ts, new_text, thread_ts=None):
    """
    Delete my original message and re-post `new_text` as me (user token).
    Returns the new message's timestamp (or None if the repost failed).
    """
    # Post first, then delete — if the post fails we haven't destroyed the original.
    kwargs = {"channel": channel_id, "text": new_text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = slack_call(user_client.chat_postMessage, **kwargs)
    if not resp or not resp.get("ts"):
        log.warning("Repost failed; leaving original message in place.")
        return None

    new_ts = resp["ts"]
    bot_msgs.mark(new_ts)        # the repost will arrive as a fresh event from me
    bot_msgs.mark(original_ts)   # belt-and-suspenders against the delete echo

    slack_call(user_client.chat_delete, channel=channel_id, ts=original_ts)
    return new_ts


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #


@app.command("/tr-setlang")
def cmd_setlang(ack, command, respond):
    ack()
    lang = command.get("text", "").strip()
    if not lang:
        respond("Usage: `/tr-setlang <lang>` — e.g. `/tr-setlang ja`")
        return
    config.set_reading_lang(command["channel_id"], lang)
    respond(f":white_check_mark: Your reading language here is now *{lang}*.")


@app.command("/tr-target")
def cmd_target(ack, command, respond):
    ack()
    lang = command.get("text", "").strip()
    if lang.lower() in ("", "auto", "off", "clear"):
        config.set_target_lang(command["channel_id"], None)
        respond(":arrows_counterclockwise: Outgoing target reset to *auto-detect*.")
        return
    config.set_target_lang(command["channel_id"], lang)
    respond(f":white_check_mark: Outgoing messages here will be translated to *{lang}*.")


@app.command("/tr-toggle")
def cmd_toggle(ack, command, respond):
    ack()
    now = config.toggle_enabled(command["channel_id"])
    respond(
        f":white_check_mark: Translation is now *{'ON' if now else 'OFF'}* "
        f"in this conversation."
    )


@app.command("/tr-status")
def cmd_status(ack, command, respond):
    ack()
    cfg = config.get(command["channel_id"])
    detected = cache.recipient_language(command["channel_id"]) or "unknown"
    respond(
        "*Translation settings for this conversation*\n"
        f"• Enabled: *{'yes' if cfg.enabled else 'no'}*\n"
        f"• My reading language (incoming): *{cfg.reading_lang}*\n"
        f"• Outgoing target: *{cfg.target_lang or f'auto (detected: {detected})'}*\n"
        f"• Default fallback target: *{DEFAULT_TARGET_LANG}*\n"
        f"• Bypass a single message with the `{RAW_PREFIX.strip()}` prefix."
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main():
    global MY_USER_ID
    auth = user_client.auth_test()  # who does the user token belong to?
    MY_USER_ID = auth["user_id"]
    log.info("Acting as user %s (%s)", MY_USER_ID, auth.get("user"))
    log.info("Translation engine: %s", translator.name)

    SocketModeHandler(app, SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()

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
# Mode:
#   "replace"  — delete my own messages and re-post them translated (one operator).
#   "annotate" — never delete anything; post a PRIVATE translation of EVERY message
#                (from anyone) to the viewer group. Works for everyone's messages
#                with a single bot.
TRANSLATION_MODE = os.environ.get("TRANSLATION_MODE", "replace").lower()
ANNOTATE = TRANSLATION_MODE == "annotate"
# Extra people who should ALSO privately see incoming translations. Comma-separated
# Slack user IDs (U…/W…) and/or @handles / display names. You are always included.
TRANSLATION_VIEWERS = os.environ.get("TRANSLATION_VIEWERS", "")
# Optional ABSOLUTE path to a shared viewer-list file read by ALL instances, so the
# viewer list is identical everywhere and edited in one place. One entry per line
# (or comma-separated); IDs and/or @handles/names. Re-read automatically when the
# file changes — no restart needed.
VIEWERS_FILE = os.environ.get("VIEWERS_FILE", "")

# --- Multi-bot (one bot per person) coordination ---------------------------- #
# Other users who run their OWN per-person bot. This instance will NOT translate
# their messages as "incoming" (their own bot handles them, by replacing them).
PEER_BOT_USERS = os.environ.get("PEER_BOT_USERS", "")
# Exactly ONE instance should be primary. The primary also translates messages
# from people who have NO bot of their own (e.g. an English-only colleague).
IS_PRIMARY = os.environ.get("IS_PRIMARY", "true").lower() == "true"

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
# Resolved at startup: set of user ids who privately see incoming translations.
VIEWER_IDS: set = set()
# Resolved at startup: set of user ids who run their own per-person bot.
PEER_IDS: set = set()


import re as _re

_USER_ID_RE = _re.compile(r"^[UW][A-Z0-9]{6,}$")


def resolve_user_spec(spec: str) -> set:
    """Turn a comma-separated spec of IDs and/or @handles/display names into a set
    of user ids. Names are resolved via users.list (needs users:read)."""
    ids = set()
    tokens = [t.strip().lstrip("@") for t in spec.split(",") if t.strip()]
    if not tokens:
        return ids

    name_to_id = {}
    if any(not _USER_ID_RE.match(t) for t in tokens):
        cursor = None
        try:
            while True:
                resp = app.client.users_list(limit=200, cursor=cursor)
                for m in resp["members"]:
                    if m.get("deleted"):
                        continue
                    p = m.get("profile", {}) or {}
                    for key in (m.get("name"), p.get("display_name"), p.get("real_name")):
                        if key:
                            name_to_id[key.lower()] = m["id"]
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as e:
            log.warning("Could not list users to resolve names: %s", e)

    for t in tokens:
        if _USER_ID_RE.match(t):
            ids.add(t)
        elif t.lower() in name_to_id:
            ids.add(name_to_id[t.lower()])
        else:
            log.warning("Could not resolve user %r — skipping", t)
    return ids


# Cache for the shared viewers file, refreshed when its mtime changes.
_viewers_file_cache = {"mtime": None, "ids": set()}


def get_viewer_ids() -> set:
    """The current viewer set: shared file (if configured) + env list + me.
    The shared file is re-read automatically whenever it changes."""
    ids = set(VIEWER_IDS)  # env-configured (resolved at startup) + me
    if VIEWERS_FILE:
        try:
            mtime = os.path.getmtime(VIEWERS_FILE)
            if _viewers_file_cache["mtime"] != mtime:
                with open(VIEWERS_FILE) as f:
                    lines = [ln.strip() for ln in f.read().splitlines()]
                # Skip blank lines and '#' comments.
                spec = ",".join(ln for ln in lines if ln and not ln.startswith("#"))
                _viewers_file_cache["ids"] = resolve_user_spec(spec)
                _viewers_file_cache["mtime"] = mtime
                log.info("Loaded %d viewer(s) from %s",
                         len(_viewers_file_cache["ids"]), VIEWERS_FILE)
            ids |= _viewers_file_cache["ids"]
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Could not read VIEWERS_FILE %s: %s", VIEWERS_FILE, e)
    ids.add(MY_USER_ID)
    return ids


def _viewers_file_load_lines():
    try:
        with open(VIEWERS_FILE) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def _viewers_file_save_lines(lines):
    with open(VIEWERS_FILE, "w") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    _viewers_file_cache["mtime"] = None  # force reload on next message


def _line_user_ids(line):
    """Resolve one non-comment file line to the set of user ids it represents."""
    s = line.strip()
    if not s or s.startswith("#"):
        return set()
    return resolve_user_spec(s)


def viewers_file_add(ids):
    lines = _viewers_file_load_lines()
    have = set()
    for ln in lines:
        have |= _line_user_ids(ln)
    new = [i for i in ids if i not in have]
    if new:
        _viewers_file_save_lines(lines + sorted(new))
    return new


def viewers_file_remove(ids):
    lines = _viewers_file_load_lines()
    kept, removed = [], set()
    for ln in lines:
        line_ids = _line_user_ids(ln)
        if line_ids & ids:
            removed |= (line_ids & ids)
            continue  # drop this entry line
        kept.append(ln)
    _viewers_file_save_lines(kept)
    return removed


def viewers_file_clear():
    # keep comments/blank lines, drop entries
    kept = [ln for ln in _viewers_file_load_lines()
            if not ln.strip() or ln.strip().startswith("#")]
    _viewers_file_save_lines(kept)


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
        u = resp["user"]
        p = u.get("profile", {}) or {}
        prof = {
            "name": p.get("display_name") or p.get("real_name") or u.get("name"),
            "icon": p.get("image_72") or p.get("image_48"),
            # Multi-channel guest -> is_restricted; single-channel guest -> is_ultra_restricted.
            "is_guest": bool(u.get("is_restricted") or u.get("is_ultra_restricted")),
            "is_bot": bool(u.get("is_bot")),
        }
    _profile_cache[user_id] = prof
    return prof


def parse_mentioned_user_ids(text: str):
    """Extract Slack user IDs from slash-command text. Handles escaped mentions
    (<@U123|name>), bare IDs (U123…), and falls back to resolving @handles /
    display names via users.list (needs users:read)."""
    ids = set()
    # Escaped mentions: <@U123> or <@U123|name>
    for uid in _re.findall(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", text):
        ids.add(uid)
    # Leftover tokens: bare IDs or names
    leftover = _re.sub(r"<@[UW][A-Z0-9]+(?:\|[^>]*)?>", " ", text)
    tokens = [t.strip().lstrip("@") for t in leftover.split() if t.strip()]
    names = [t for t in tokens if _USER_ID_RE.match(t) is None]
    for t in tokens:
        if _USER_ID_RE.match(t):
            ids.add(t)
    if names:
        name_to_id = {}
        cursor = None
        try:
            while True:
                resp = app.client.users_list(limit=200, cursor=cursor)
                for m in resp["members"]:
                    if m.get("deleted"):
                        continue
                    p = m.get("profile", {}) or {}
                    for key in (m.get("name"), p.get("display_name"), p.get("real_name")):
                        if key:
                            name_to_id[key.lower()] = m["id"]
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as e:
            log.warning("Could not list users to resolve names: %s", e)
        for t in names:
            if t.lower() in name_to_id:
                ids.add(name_to_id[t.lower()])
    return ids


# --------------------------------------------------------------------------- #
# Core: message event handler
# --------------------------------------------------------------------------- #


@app.event("message")
def handle_message(event, logger):
    subtype = event.get("subtype")

    # Edited messages arrive as `message_changed` — re-translate them.
    if subtype == "message_changed":
        _handle_edit(event)
        return

    # Allow plain messages and file uploads (file_share carries a caption + file);
    # ignore other non-plain messages (deletes, joins, etc.) and bot-authored ones.
    if subtype not in (None, "file_share"):
        return
    if event.get("bot_id"):
        return

    channel_id = event.get("channel")
    ts = event.get("ts")
    user = event.get("user")
    text = event.get("text", "")
    is_file = subtype == "file_share" or bool(event.get("files"))

    # No text to translate (e.g. a file with no caption) — nothing to do.
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

    if ANNOTATE:
        # Annotation mode: translate EVERY message (anyone's, including mine) into
        # the viewers' reading language and post it privately. Nothing is deleted.
        _handle_incoming(channel_id, ts, user, text, cfg.reading_lang, event.get("thread_ts"))
    elif is_me:
        _handle_outgoing(channel_id, ts, text, event.get("thread_ts"), is_file=is_file)
    elif IS_PRIMARY and user not in PEER_IDS:
        # Only the primary bot translates messages from people without their own
        # bot; a peer's messages are handled by that peer's own bot (as outgoing).
        _handle_incoming(channel_id, ts, user, text, cfg.reading_lang, event.get("thread_ts"))


def _handle_edit(event):
    """A message was edited (`message_changed`). Re-translate the new text."""
    msg = event.get("message") or {}
    prev = event.get("previous_message") or {}

    # Skip bot-authored edits and non-text changes (e.g. link unfurls fire
    # message_changed with the text unchanged).
    if msg.get("bot_id"):
        return
    new_text = msg.get("text", "")
    if not new_text or new_text == prev.get("text", ""):
        return

    channel_id = event.get("channel")
    mts = msg.get("ts")           # the edited message's own timestamp
    user = msg.get("user")
    thread_ts = msg.get("thread_ts")
    if not channel_id or not mts or not user:
        return

    cfg = config.get(channel_id)
    if not cfg.enabled:
        return

    # Skip our own posts/updates (reposts, and the in-place caption updates we make
    # for file messages) so the resulting message_changed echo doesn't loop.
    if bot_msgs.is_ours(mts):
        return

    is_file = bool(msg.get("files"))
    if ANNOTATE:
        _handle_incoming(channel_id, mts, user, new_text, cfg.reading_lang, thread_ts, edited=True)
    elif user == MY_USER_ID:
        _handle_outgoing(channel_id, mts, new_text, thread_ts, is_file=is_file)
    elif IS_PRIMARY and user not in PEER_IDS:
        _handle_incoming(channel_id, mts, user, new_text, cfg.reading_lang, thread_ts, edited=True)


def _handle_incoming(channel_id, ts, user, text, reading_lang, thread_ts=None, edited=False):
    """Translate a message for the viewers and post it privately.

    Translates into the reading language; but if the message is ALREADY in the
    reading language, translate into the OTHER configured language instead (so a
    Vietnamese message shows as English and vice-versa). This bidirectional
    fallback only applies in annotation mode."""
    context = build_context(channel_id)
    other_lang = config.get(channel_id).target_lang or DEFAULT_TARGET_LANG

    result = translator.translate(text, target_lang=reading_lang, context=context)
    detected = result.detected_lang if result else None
    shown_lang = reading_lang

    if (ANNOTATE and result is not None and not result.needs_translation
            and other_lang and other_lang.lower() != reading_lang.lower()):
        alt = translator.translate(text, target_lang=other_lang, context=context)
        if alt is not None:
            result, shown_lang = alt, other_lang
            detected = alt.detected_lang or detected

    cache.add(
        channel_id,
        CachedMessage(user=user, text=text, is_me=(user == MY_USER_ID), detected_lang=detected),
    )

    if result is None or not result.needs_translation:
        return  # already in both configured languages, or translation failed

    # Private (ephemeral) note — shown inside the sender's message thread to me and
    # to the channel's chosen viewers (managed with /tr-viewers). One ephemeral per
    # recipient (Slack ephemerals target a single user). Each viewer must be a
    # channel member, and (like all ephemerals) sees it only while the channel/thread
    # is open.
    reply_root = thread_ts or ts
    prof = get_profile(user)
    tag = " · ✏️ edited" if edited else ""
    body = f":speech_balloon: ({detected} → {shown_lang}{tag})\n{result.translated_text}"
    base = {"channel": channel_id, "text": body, "thread_ts": reply_root}
    if prof and prof.get("name"):
        base["username"] = prof["name"]        # show the sender's name as the label
        if prof.get("icon"):
            base["icon_url"] = prof["icon"]

    # Shared file is the single source of truth when configured; otherwise fall
    # back to the per-channel DB list.
    recipients = get_viewer_ids()
    if not VIEWERS_FILE:
        recipients |= set(config.list_viewers(channel_id))
    for viewer in recipients:
        slack_call(app.client.chat_postEphemeral, user=viewer, **base)


def _handle_outgoing(channel_id, ts, text, thread_ts=None, is_file=False):
    """
    I posted -> show the translated version as me.
    Target = forced per-conversation target, else recipients' detected language,
    else the configured default.

    Normal messages are deleted and re-posted translated. Messages with a file
    attachment are instead updated IN PLACE (chat.update on the caption), because
    deleting them would also delete the file. If the message was a threaded reply,
    the repost goes back into the same thread.
    """
    cfg = config.get(channel_id)

    # Only treat it as a thread reply when thread_ts differs from the message's own
    # ts (a thread *parent* has thread_ts == ts, and we don't want to re-parent it).
    reply_thread = thread_ts if thread_ts and thread_ts != ts else None

    def _apply(new_body):
        """Replace my message with new_body; returns the anchor ts for the note."""
        if is_file:
            # Keep the file — just rewrite its caption. Mark the ts so the
            # resulting message_changed echo from our own update is ignored.
            bot_msgs.mark(ts)
            slack_call(user_client.chat_update, channel=channel_id, ts=ts, text=new_body)
            return ts
        return _replace_my_message(channel_id, ts, new_body, reply_thread)

    # Bypass: `!raw ` sends untranslated (we still strip the prefix).
    if text.startswith(RAW_PREFIX):
        raw_body = text[len(RAW_PREFIX):].lstrip()
        _apply(raw_body)
        cache.add(channel_id, CachedMessage(user=MY_USER_ID, text=raw_body, is_me=True))
        return

    target = cfg.target_lang or cache.recipient_language(channel_id) or DEFAULT_TARGET_LANG

    context = build_context(channel_id)
    result = translator.translate(text, target_lang=target, context=context)

    cache.add(channel_id, CachedMessage(user=MY_USER_ID, text=text, is_me=True))

    if result is None or not result.needs_translation:
        return  # already in the recipients' language, or translation failed

    new_ts = _apply(result.translated_text)

    # Privately show my ORIGINAL text (the viewers' language) to me and to every
    # viewer, inside the translated message's thread. The public message is the
    # translation; viewers read the original here. (Open the thread to see it.)
    note_thread = reply_thread or new_ts
    base = {"channel": channel_id, "text": f":speech_balloon: {text}"}
    if note_thread:
        base["thread_ts"] = note_thread
    me_prof = get_profile(MY_USER_ID)
    if me_prof and me_prof.get("name"):
        base["username"] = me_prof["name"]     # label it as me (the sender)
        if me_prof.get("icon"):
            base["icon_url"] = me_prof["icon"]
    for viewer in get_viewer_ids():
        slack_call(app.client.chat_postEphemeral, user=viewer, **base)


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
    viewers = config.list_viewers(command["channel_id"])
    viewer_str = ", ".join(f"<@{v}>" for v in viewers) if viewers else "just me"
    respond(
        "*Translation settings for this conversation*\n"
        f"• Enabled: *{'yes' if cfg.enabled else 'no'}*\n"
        f"• My reading language (incoming): *{cfg.reading_lang}*\n"
        f"• Outgoing target: *{cfg.target_lang or f'auto (detected: {detected})'}*\n"
        f"• Default fallback target: *{DEFAULT_TARGET_LANG}*\n"
        f"• Who privately sees translations here: {viewer_str}\n"
        f"• Bypass a single message with the `{RAW_PREFIX.strip()}` prefix."
    )


@app.command("/tr-viewers")
def cmd_viewers(ack, command, respond):
    """Choose who privately sees incoming translations in this channel.
    Usage: /tr-viewers add @a @b | remove @a | list | clear"""
    ack()
    channel_id = command["channel_id"]
    parts = command.get("text", "").strip().split(maxsplit=1)
    action = (parts[0].lower() if parts else "list")
    rest = parts[1] if len(parts) > 1 else ""

    # When a shared viewers file is configured it is the single source of truth
    # (and changes apply to ALL bots/channels). Otherwise use the per-channel DB.
    use_file = bool(VIEWERS_FILE)

    if action in ("list", ""):
        viewers = sorted(get_viewer_ids()) if use_file else config.list_viewers(channel_id)
        scope = "everywhere" if use_file else "here"
        if viewers:
            respond(f"Privately see translations {scope}: " + ", ".join(f"<@{v}>" for v in viewers))
        else:
            respond(f"Only *you* see translations {scope}. Add people with `/tr-viewers add @name`.")
    elif action == "add":
        ids = parse_mentioned_user_ids(rest)
        if not ids:
            respond("Couldn't find anyone in that. Try `/tr-viewers add @alice @bob`.")
            return
        if use_file:
            viewers_file_add(ids)
        else:
            config.add_viewers(channel_id, ids)
        respond(":white_check_mark: Added: " + ", ".join(f"<@{i}>" for i in ids))
    elif action == "remove":
        ids = parse_mentioned_user_ids(rest)
        if not ids:
            respond("Couldn't find anyone in that. Try `/tr-viewers remove @alice`.")
            return
        if use_file:
            viewers_file_remove(ids)
        else:
            config.remove_viewers(channel_id, ids)
        respond(":white_check_mark: Removed: " + ", ".join(f"<@{i}>" for i in ids))
    elif action == "clear":
        if use_file:
            viewers_file_clear()
        else:
            config.clear_viewers(channel_id)
        respond(":white_check_mark: Cleared — only you see translations now.")
    else:
        respond("Usage: `/tr-viewers add @a @b` | `remove @a` | `list` | `clear`")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main():
    global MY_USER_ID, VIEWER_IDS, PEER_IDS
    auth = user_client.auth_test()  # who does the user token belong to?
    MY_USER_ID = auth["user_id"]
    log.info("Acting as user %s (%s)", MY_USER_ID, auth.get("user"))
    log.info("Translation engine: %s | mode: %s", translator.name, TRANSLATION_MODE)

    VIEWER_IDS = resolve_user_spec(TRANSLATION_VIEWERS) | {MY_USER_ID}
    PEER_IDS = resolve_user_spec(PEER_BOT_USERS)
    log.info("Primary: %s | viewers: %s | peer bots: %s",
             IS_PRIMARY,
             ", ".join(sorted(get_viewer_ids())),   # includes the shared file
             ", ".join(sorted(PEER_IDS)) or "(none)")

    SocketModeHandler(app, SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()

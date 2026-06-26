# Slack Auto-Translator

Context-aware, two-way Slack translation powered by [Slack Bolt](https://slack.dev/bolt-python/)
(Socket Mode) and the [Anthropic Claude API](https://platform.claude.com/).

It runs with **your** Slack user token so it can act as you:

- **Incoming** (others → you): when someone posts in another language, it posts the
  translation as a threaded reply (or an ephemeral note) beneath the original. The
  original stays visible.
- **Outgoing** (you → others): when **you** post, it deletes your message and
  re-posts the translated version *in your place*, so it appears as you.

Translation is context-aware — recent conversation history is passed to Claude so
tone, names, slang, and pronouns come out naturally. Per-conversation settings let
you read different languages in different channels.

---

## ⚠️ Read this first: what Slack does and doesn't allow

| Capability | Status | Notes |
|---|---|---|
| Delete **your own** messages | ✅ Works | `chat.delete` with a user token (`xoxp`) succeeds on messages you authored. |
| Re-post **as you** | ✅ Works | `chat.postMessage` with the user token posts under your identity. |
| Translate in **channels / group DMs the bot is in** | ✅ Works | Both directions. Invite the bot to the channel. |
| Translate in your **1:1 DMs with other people** | ❌ Not achievable (event-driven) | **Platform limitation.** The Events API (which Socket Mode uses) only delivers message events for conversations the **bot** belongs to. A bot can't join a 2-person DM, so those events never arrive. |

This is a genuine Slack constraint, not a shortcoming of this code. The
event-driven design (what `slack_bolt` + Socket Mode is built for) covers every
channel and multi-person DM (`mpim`) the bot is a member of, and **cleanly skips**
anything it can't see.

**Closest workable alternative for true 1:1-DM coverage:** poll
`conversations.history` with your **user token** on a timer instead of subscribing
to events (the user token *can* read your own DM history via `im:history`). That
trades real-time delivery for coverage and a more complex cursor/dedup loop. The
code is structured so this could be added as a second ingestion path; it is not
implemented here because it changes the delivery model the task specified
(Bolt + Socket Mode events).

> Also note: legacy RTM (which *could* stream a user's own DMs via a user token) is
> deprecated and not supported by Bolt's Socket Mode. There is no supported modern
> path to receive 1:1-DM events for arbitrary conversations.

---

## How it works

```
                       Slack Events API (Socket Mode)
                                   │  message events
                                   ▼
                          ┌─────────────────┐
   incoming (others) ───▶ │    app.py       │ ───▶ threaded reply / ephemeral
                          │  message router │       (bot token)
   outgoing (me)    ───▶  │                 │ ───▶ delete + repost as me
                          └────────┬────────┘       (USER token)
                                   │
                  ┌────────────────┼─────────────────┐
                  ▼                ▼                  ▼
            translator.py      store.py          loop guard
          (Claude API:      (SQLite config +   (skip our own
           detect+translate, in-mem cache)      reposted ts)
           structured JSON)
```

- **Loop prevention** — every timestamp the app posts or reposts is remembered. A
  re-posted outgoing message arrives back as a fresh event from *your* user id; the
  guard recognizes its `ts` and skips it.
- **Skip when not needed** — Claude returns `needs_translation: false` when the text
  is already in the target language, so we never delete/repost or thread a reply
  needlessly.
- **Bypass** — prefix an outgoing message with `!raw ` to send it untranslated (the
  prefix is stripped before reposting).

### Files

| File | Purpose |
|---|---|
| `app.py` | Bolt app: Socket Mode wiring, message routing, slash commands. |
| `translator.py` | Claude API wrapper: detection + context-aware translation, structured JSON output, error handling. |
| `store.py` | Per-conversation SQLite config, in-memory message cache, loop-prevention tracker. |

---

## Slack app setup

### 1. Create the app

Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.

### 2. Enable Socket Mode

**Settings → Socket Mode → Enable**. This generates an **App-Level Token** (`xapp-…`)
with the `connections:write` scope → this is your `SLACK_APP_TOKEN`.

### 3. OAuth scopes

**Features → OAuth & Permissions.**

**Bot Token Scopes** (the bot receives events and posts incoming translations):

- `channels:history`, `groups:history`, `im:history`, `mpim:history` — receive messages
- `chat:write` — post threaded replies / ephemeral notes
- `commands` — slash commands
- `reactions:read` — (optional) if you extend bypass to a reaction trigger

**User Token Scopes** (the app acts as you):

- `chat:write` — post **and delete** your own messages (covers re-posting as you and
  `chat.delete` on your own messages)
- `channels:history`, `groups:history`, `im:history`, `mpim:history` — read history
  for context / language detection
- `reactions:read` — (optional) reaction-based bypass

> `chat.delete` of your own messages is covered by the user-token `chat:write`
> scope; there is no separate delete scope for self-authored messages.

### 4. Event subscriptions

**Features → Event Subscriptions → Enable.** With Socket Mode on, you don't need a
public Request URL. Under **Subscribe to bot events**, add:

- `message.channels`
- `message.groups`
- `message.im`
- `message.mpim`

### 5. Slash commands

**Features → Slash Commands → Create New Command** for each (Request URL is ignored
in Socket Mode — put any placeholder like `https://example.com/slack`):

| Command | Description |
|---|---|
| `/tr-setlang` | Set my reading language for the current conversation |
| `/tr-target` | Force the outgoing target language here (`auto` to clear) |
| `/tr-toggle` | Enable/disable translation in this conversation |
| `/tr-status` | Show current settings |

### 6. Install & grab tokens

**Install App** → authorize. Then collect:

- **Bot User OAuth Token** (`xoxb-…`) → `SLACK_BOT_TOKEN`
- **User OAuth Token** (`xoxp-…`) → `SLACK_USER_TOKEN`
- **App-Level Token** (`xapp-…`, from step 2) → `SLACK_APP_TOKEN`

> If you add scopes after first install, **reinstall** the app to mint new tokens.

### 7. Invite the bot

In each channel you want translated: `/invite @YourBot`. (Required — the bot only
receives events for conversations it's a member of.)

---

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env       # then fill in your tokens + ANTHROPIC_API_KEY

python check_setup.py      # validates tokens, scopes, and API key before going live
python app.py
```

`check_setup.py` authenticates both Slack tokens (`auth.test`), opens a Socket
Mode connection with the app token, checks for missing OAuth scopes, and verifies
your Anthropic key + model — turning a misconfiguration into a clear checklist
instead of a stack trace.

You should see `Acting as user U… ` in the logs — that confirms the user token
resolved to you. The app stays connected over Socket Mode; no public URL needed.

---

## Usage

| Action | How |
|---|---|
| Set your reading language for a channel | `/tr-setlang ja` |
| Force outgoing target (e.g. always Vietnamese) | `/tr-target vi` |
| Reset outgoing target to auto-detect | `/tr-target auto` |
| Turn translation on/off here | `/tr-toggle` |
| See current settings | `/tr-status` |
| Send one message untranslated | start it with `!raw ` |

Languages can be ISO codes (`en`, `ja`, `vi`) or names (`Japanese`) — Claude
interprets either; ISO codes are recommended for the cleanest detection matching.

---

## Translation engine: free vs paid

Set `TRANSLATION_ENGINE` in `.env`:

| Engine | Cost | Context-aware? | Notes |
|---|---|---|---|
| `google` | **Free** | No | `deep-translator` + `langdetect`, no API key. Translates each message in isolation. Slack `@mentions`, `:emoji:`, links and `` `code` `` are masked before translation and restored after (best-effort). Online; the free Google endpoint can rate-limit under heavy use. |
| `claude` | ~$2–3 / 1,000 msgs | Yes | Anthropic API. Reads recent conversation so tone, slang, names, and pronouns translate naturally. Needs `ANTHROPIC_API_KEY`. |

You can switch engines any time by editing `.env` and restarting — no code changes. `check_setup.py` validates whichever engine you selected.

## Configuration reference

All via `.env` (see `.env.example`): `ANTHROPIC_MODEL`, `DEFAULT_READING_LANG`,
`DEFAULT_TARGET_LANG`, `DB_PATH`, `CONTEXT_MESSAGES`, `RAW_PREFIX`,
`INCOMING_EPHEMERAL`, `LOG_LEVEL`.

---

## Notes, trade-offs & extension points

- **Engine** — `TRANSLATION_ENGINE=google` (free) or `claude` (paid, context-aware).
  See the table above.
- **Model** (Claude engine) — defaults to `claude-sonnet-4-6` (fast/economical for
  chat volume). Set `ANTHROPIC_MODEL=claude-opus-4-8` for maximum quality. Thinking
  is disabled in the translation call to keep latency low.
- **Rate limits & retries** — the Anthropic SDK retries 429/5xx automatically;
  `translator.py` adds extra retry headroom and degrades to "leave the message
  untouched" on hard failure. Slack 429s are retried using `Retry-After`.
- **Cost** — one Claude call per processed message. Caching is in-memory; recent
  messages double as translation context.
- **State** — per-conversation settings persist in SQLite; the message cache and
  loop-prevention set are in-memory and reset on restart (safe — they only affect
  context quality and dedup of in-flight reposts).
- **Outgoing safety** — we **post the translation first, then delete the original**,
  so a failed repost never destroys your message.
- **Possible extensions**: reaction-based bypass (`reactions:read` is already
  scoped), a polling ingestion path for 1:1 DMs (see the limitation section), and
  persisting detected languages across restarts.

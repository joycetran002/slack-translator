"""
store.py
========

Two responsibilities:

1. Per-conversation configuration, persisted in a small SQLite database:
     - reading_lang   : the language *I* want to read incoming messages in
     - target_lang    : a forced outgoing target (overrides auto-detection)
     - enabled        : whether translation is active in this conversation

2. In-memory state that only needs to live as long as the process:
     - a short rolling cache of recent messages per conversation (used both as
       translation *context* and for detecting the recipient's language)
     - a bounded set of message timestamps the bot itself posted / reposted,
       used to break the translate -> post -> event -> translate loop.

The SQLite layer is intentionally tiny and synchronous. Slack Bolt dispatches
each event/command on a worker thread, so we guard DB access with a lock.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Per-conversation configuration (SQLite)
# --------------------------------------------------------------------------- #


@dataclass
class ConvConfig:
    """Resolved settings for a single Slack conversation (channel / DM / group)."""

    channel_id: str
    reading_lang: Optional[str]  # language I read incoming messages in
    target_lang: Optional[str]   # forced outgoing target, if any
    enabled: bool


class ConfigStore:
    """Per-conversation settings backed by SQLite."""

    def __init__(self, db_path: str, default_reading_lang: str, default_target_lang: str):
        self.default_reading_lang = default_reading_lang
        self.default_target_lang = default_target_lang
        self._lock = threading.Lock()
        # check_same_thread=False: Bolt calls us from worker threads; the lock
        # above serializes access so this is safe.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    channel_id   TEXT PRIMARY KEY,
                    reading_lang TEXT,
                    target_lang  TEXT,
                    enabled      INTEGER NOT NULL DEFAULT 1
                )
                """
            )

    def get(self, channel_id: str) -> ConvConfig:
        """Return settings for a conversation, falling back to defaults."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        if row is None:
            return ConvConfig(
                channel_id=channel_id,
                reading_lang=self.default_reading_lang,
                target_lang=None,
                enabled=True,
            )
        return ConvConfig(
            channel_id=channel_id,
            reading_lang=row["reading_lang"] or self.default_reading_lang,
            target_lang=row["target_lang"],
            enabled=bool(row["enabled"]),
        )

    def _upsert(self, channel_id: str, **fields) -> None:
        """Insert or update selected columns for a conversation."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO conversations (channel_id) VALUES (?)",
                (channel_id,),
            )
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self._conn.execute(
                f"UPDATE conversations SET {assignments} WHERE channel_id = ?",
                (*fields.values(), channel_id),
            )

    def set_reading_lang(self, channel_id: str, lang: str) -> None:
        self._upsert(channel_id, reading_lang=lang)

    def set_target_lang(self, channel_id: str, lang: Optional[str]) -> None:
        self._upsert(channel_id, target_lang=lang)

    def toggle_enabled(self, channel_id: str) -> bool:
        """Flip the enabled flag and return the new value."""
        current = self.get(channel_id).enabled
        self._upsert(channel_id, enabled=0 if current else 1)
        return not current


# --------------------------------------------------------------------------- #
# In-memory message cache (context + recipient-language detection)
# --------------------------------------------------------------------------- #


@dataclass
class CachedMessage:
    user: str
    text: str
    is_me: bool
    detected_lang: Optional[str] = None  # filled in once we know it


class MessageCache:
    """Rolling per-conversation cache of the most recent messages."""

    def __init__(self, max_per_conversation: int = 20):
        self._max = max_per_conversation
        self._by_channel: Dict[str, Deque[CachedMessage]] = {}
        self._lock = threading.Lock()

    def add(self, channel_id: str, msg: CachedMessage) -> None:
        with self._lock:
            dq = self._by_channel.setdefault(channel_id, deque(maxlen=self._max))
            dq.append(msg)

    def recent(self, channel_id: str, limit: Optional[int] = None) -> List[CachedMessage]:
        with self._lock:
            dq = self._by_channel.get(channel_id)
            if not dq:
                return []
            items = list(dq)
        return items[-limit:] if limit else items

    def recipient_language(self, channel_id: str) -> Optional[str]:
        """
        Best guess at the language the *other* participants are writing in:
        the most common detected language among recent non-me messages.
        Returns None if we have no detected languages cached yet.
        """
        counts: Dict[str, int] = {}
        for m in self.recent(channel_id):
            if not m.is_me and m.detected_lang:
                counts[m.detected_lang] = counts.get(m.detected_lang, 0) + 1
        if not counts:
            return None
        return max(counts, key=counts.get)


# --------------------------------------------------------------------------- #
# Loop prevention: timestamps the bot posted / reposted
# --------------------------------------------------------------------------- #


class BotMessageTracker:
    """
    Remembers message timestamps that originated from this app (threaded
    translations, or messages we deleted-and-reposted as the user).

    When Slack later delivers a `message` event for one of those timestamps,
    we recognize it and skip re-processing — otherwise an outgoing repost
    (which arrives as a fresh message event from *my* user id) would be
    translated again, forever.

    Bounded with an OrderedDict acting as an LRU set so memory stays flat.
    """

    def __init__(self, max_size: int = 5000):
        self._max = max_size
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.Lock()

    def mark(self, ts: str) -> None:
        with self._lock:
            self._seen[ts] = None
            self._seen.move_to_end(ts)
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)

    def is_ours(self, ts: str) -> bool:
        with self._lock:
            return ts in self._seen

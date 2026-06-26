"""
translator.py
=============

Pluggable translation backends with a common interface:

    translate(text, target_lang, context) -> TranslationResult | None
    detect_language(text)                  -> ISO-639-1 code | None

Two implementations, selected by the TRANSLATION_ENGINE env var:

  * "claude"  (default) — Anthropic Claude API. Context-aware: passes recent
    conversation history so tone, slang, names, and pronouns translate naturally.
    Paid (~$2-3 per 1,000 messages on claude-sonnet-4-6).

  * "google"  — deep-translator's free Google endpoint + langdetect. No API key,
    no signup, $0. Translates each message in isolation (NO conversation context),
    so nuance is lower. Slack entities (@mentions, :emoji:, links, `code`) are
    masked before translation and restored after, on a best-effort basis.

Use build_translator() to get the configured backend.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger("translator")


@dataclass
class TranslationResult:
    detected_lang: str
    needs_translation: bool
    translated_text: str


@dataclass
class ContextLine:
    text: str
    is_me: bool


# --------------------------------------------------------------------------- #
# Backend 1: Claude (context-aware, paid)
# --------------------------------------------------------------------------- #

_TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "detected_lang": {
            "type": "string",
            "description": "ISO 639-1 code of the source language, e.g. 'en', 'ja', 'vi'.",
        },
        "needs_translation": {
            "type": "boolean",
            "description": "False if the text is already in the target language.",
        },
        "translated_text": {
            "type": "string",
            "description": "The translation, or the original text unchanged if needs_translation is false.",
        },
    },
    "required": ["detected_lang", "needs_translation", "translated_text"],
    "additionalProperties": False,
}

_DETECT_SCHEMA = {
    "type": "object",
    "properties": {"lang": {"type": "string", "description": "ISO 639-1 code."}},
    "required": ["lang"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """You are an expert real-time translator embedded in a Slack workspace.

Rules:
- Detect the source language of the message to translate.
- Translate it naturally into the target language, matching the tone and register \
of a chat conversation (casual stays casual, formal stays formal).
- Use the surrounding conversation history (when provided) to resolve pronouns, \
names, slang, and ambiguous references so the translation reads naturally.
- PRESERVE EXACTLY, without translating or altering them:
    * Markdown / Slack formatting (*bold*, _italic_, ~strike~, `code`, ```blocks```)
    * emoji and :emoji_shortcodes:
    * @mentions and <@USERID> / <#CHANNELID> references
    * URLs and links
    * numbers, code identifiers, and file paths
- If the message is ALREADY in the target language, set needs_translation to false \
and return translated_text equal to the original text.
- Return ONLY the structured fields. No preamble, no explanation, no quotes around \
the translation."""


class ClaudeTranslator:
    name = "claude"

    def __init__(self, model: str, api_key: Optional[str] = None, max_tokens: int = 4096):
        import anthropic  # lazy: only needed for this backend

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=4)
        self._model = model
        self._max_tokens = max_tokens

    def _format_context(self, context: List[ContextLine]) -> str:
        if not context:
            return "(no prior context)"
        return "\n".join(f"[{'ME' if c.is_me else 'OTHER'}] {c.text}" for c in context)

    def _call(self, *, schema: dict, user_content: str) -> Optional[dict]:
        import json

        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_SYSTEM_PROMPT,
                # Translation is latency-sensitive, not a reasoning task.
                thinking={"type": "disabled"},
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": user_content}],
            )
        except self._anthropic.APIError as e:
            log.warning("Anthropic API error, skipping translation: %s", e)
            return None

        if resp.stop_reason == "refusal":
            log.warning("Translation refused by safety system; leaving text untouched.")
            return None

        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("Could not parse model JSON: %r", text)
            return None

    def translate(self, text, target_lang, context=None) -> Optional[TranslationResult]:
        context = context or []
        user_content = (
            f"Target language: {target_lang}\n\n"
            f"Recent conversation (most recent last):\n"
            f"{self._format_context(context)}\n\n"
            f"Message to translate:\n{text}"
        )
        data = self._call(schema=_TRANSLATE_SCHEMA, user_content=user_content)
        if data is None:
            return None
        return TranslationResult(
            detected_lang=str(data.get("detected_lang", "")).lower(),
            needs_translation=bool(data.get("needs_translation", True)),
            translated_text=data.get("translated_text", text),
        )

    def detect_language(self, text) -> Optional[str]:
        data = self._call(
            schema=_DETECT_SCHEMA,
            user_content=f"Identify the language of this message:\n{text}",
        )
        if data is None:
            return None
        return str(data.get("lang", "")).lower() or None


# --------------------------------------------------------------------------- #
# Backend 2: Google via deep-translator (free, no context)
# --------------------------------------------------------------------------- #

# Common language-name -> ISO 639-1, used only to decide "already in target?".
# deep-translator itself accepts both codes and names as the target.
_NAME_TO_CODE = {
    "english": "en", "japanese": "ja", "vietnamese": "vi", "korean": "ko",
    "chinese": "zh-CN", "spanish": "es", "french": "fr", "german": "de",
    "portuguese": "pt", "italian": "it", "russian": "ru", "arabic": "ar",
    "hindi": "hi", "thai": "th", "indonesian": "id", "dutch": "nl",
    "polish": "pl", "turkish": "tr", "ukrainian": "uk",
}

# Common but invalid codes people type, mapped to what Google expects.
_ALIASES = {
    "jp": "ja", "kr": "ko", "cn": "zh-CN", "zh": "zh-CN",
    "ua": "uk", "cz": "cs", "gr": "el", "vn": "vi",
}


def _canonical_lang(lang: str) -> str:
    """Normalize a user-supplied code/name to a code Google accepts."""
    l = lang.strip().lower()
    if l in _NAME_TO_CODE:
        return _NAME_TO_CODE[l]
    return _ALIASES.get(l, l)

# Slack entities + code spans we mask out so the translator leaves them intact.
# Order matters: code blocks before inline code before the rest.
_PROTECT_PATTERNS = [
    re.compile(r"```.*?```", re.DOTALL),   # code blocks
    re.compile(r"`[^`]+`"),                # inline code
    re.compile(r"<[^>\s][^>]*>"),          # <@U..>, <#C..|name>, <!..>, <http..|label>
    re.compile(r":[a-z0-9_+\-']+:"),       # :emoji_shortcodes:
]
# Single guillemets are rarely altered by translators, so they make safe sentinels.
_RESTORE_RE = re.compile(r"‹(\d+)›")

# Slack mrkdwn inline formatting: *bold*, _italic_, ~strike~. Google strips these
# markers if it translates the whole string, so we split on them, translate the
# inner text, and re-wrap. (Slack has no underline.)
# Outer capturing group is required so re.split() KEEPS the spans.
_FORMAT_RE = re.compile(r"(\*[^*\n]+\*|_[^_\n]+_|~[^~\n]+~)")
# Split any chunk into (leading ws, core, trailing ws) so spacing survives.
_WS_RE = re.compile(r"^(\s*)(.*?)(\s*)$", re.DOTALL)


class GoogleTranslatorBackend:
    name = "google"

    def __init__(self):
        # Lazy imports: only this backend needs these packages.
        from deep_translator import GoogleTranslator  # noqa: F401
        import langdetect  # noqa: F401

        self._GoogleTranslator = GoogleTranslator
        self._langdetect = langdetect
        self._warned_context = False

    # ---- entity protection -------------------------------------------- #

    def _protect(self, text: str):
        tokens: List[str] = []

        def _stash(m):
            tokens.append(m.group(0))
            return f"‹{len(tokens) - 1}›"

        masked = text
        for pat in _PROTECT_PATTERNS:
            masked = pat.sub(_stash, masked)
        return masked, tokens

    def _restore(self, text: str, tokens: List[str]) -> str:
        return _RESTORE_RE.sub(lambda m: tokens[int(m.group(1))], text)

    # ---- interface ---------------------------------------------------- #

    def detect_language(self, text) -> Optional[str]:
        try:
            return self._langdetect.detect(text).lower()
        except Exception:
            return None  # too short / undetectable

    def translate(self, text, target_lang, context=None) -> Optional[TranslationResult]:
        if context and not self._warned_context:
            log.info("Google engine ignores conversation context (translates per-message).")
            self._warned_context = True

        src = self.detect_language(text)
        target_code = _canonical_lang(target_lang)

        # Skip if we can tell it's already in the target language.
        if src and src.lower() == target_code.lower():
            return TranslationResult(detected_lang=src, needs_translation=False, translated_text=text)

        masked, tokens = self._protect(text)
        if not masked.strip():  # message was nothing but entities/emoji
            return TranslationResult(detected_lang=src or "", needs_translation=False, translated_text=text)

        # Split into formatted spans (*bold*, _italic_, ~strike~) and plain text.
        # Translate each piece's core text, then re-wrap formatted ones so the
        # markers survive. Surrounding whitespace is preserved per-piece (Google
        # trims each item, which would otherwise collapse the spaces between
        # words). Pieces with no letters are kept verbatim and never translated.
        parts = []           # ("lit", literal) | ("trans", marker, lead, trail, index)
        texts: List[str] = []
        for seg in _FORMAT_RE.split(masked):
            if not seg:
                continue
            fm = _FORMAT_RE.fullmatch(seg)
            marker = seg[0] if fm else ""
            body = seg[1:-1] if fm else seg
            lead, core, trail = _WS_RE.match(body).groups()
            if any(ch.isalpha() for ch in core):
                parts.append(("trans", marker, lead, trail, len(texts)))
                texts.append(core)
            else:
                parts.append(("lit", seg))

        try:
            gt = self._GoogleTranslator(source="auto", target=target_code)
            results = [gt.translate(texts[0])] if len(texts) == 1 else (
                gt.translate_batch(texts) if texts else []
            )
        except Exception as e:
            log.warning("Google translation failed, leaving text untouched: %s", e)
            return None

        out = []
        for p in parts:
            if p[0] == "lit":
                out.append(p[1])
            else:
                _, marker, lead, trail, idx = p
                piece = (results[idx] or texts[idx]).strip()
                wrapped = f"{marker}{piece}{marker}" if marker else piece
                out.append(f"{lead}{wrapped}{trail}")

        restored = self._restore("".join(out), tokens)
        return TranslationResult(
            detected_lang=src or "",
            needs_translation=True,
            translated_text=restored,
        )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_translator():
    """Construct the backend named by TRANSLATION_ENGINE (default: claude)."""
    engine = os.environ.get("TRANSLATION_ENGINE", "claude").lower()
    if engine == "google":
        return GoogleTranslatorBackend()
    if engine == "claude":
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        return ClaudeTranslator(model=model)
    raise ValueError(f"Unknown TRANSLATION_ENGINE: {engine!r} (use 'claude' or 'google')")

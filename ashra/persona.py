"""Persona, voice, and small talk.

Personality lives here, separately from the grounding rules.  The assistant may
sound like a dry British butler, but it must still answer only from the notes.
Keeping the two blocks apart means the tone can be changed without weakening the
grounding constraint.
"""

from __future__ import annotations

import re
from datetime import datetime

PERSONA = """
You are A.S.H.R.A, the private assistant for a personal knowledge galaxy.
Your manner is that of a dry, unflappable British butler: precise, quietly
amused, and never chatty.
A good answer is usually two sentences of fact followed by one short, dry aside.
Address the user as "sir" at most once per answer, and only when it sounds
natural.
"""

GROUNDING_RULES = """
Answer ONLY from the notes supplied below. Never use outside knowledge and never
invent details.
Answer in at most three sentences and 65 words. Lead with the facts.
Close with one short, understated dry line in character. Keep it brief enough
that it never delays or replaces the answer, and never sacrifice accuracy for a
joke.
Do not recite the notes back. Do not mention note numbers, brackets, or file
paths. Name a note only when it genuinely helps.
If the notes do not cover the question, say so plainly.
"""


def compose_system_prompt(notes: str) -> str:
    """Build the full system prompt from persona, rules, and note excerpts."""
    return (
        f"{PERSONA.strip()}\n\n"
        f"{GROUNDING_RULES.strip()}\n\n"
        "NOTES\n"
        f"{notes.strip()}\n\n"
        "Answer using only the notes above."
    )


def _normalize(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"\ba\.?s\.?h\.?r\.?a\b", " ", lowered)
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    words = [word for word in lowered.split() if word not in {"ashra", "sir", "please"}]
    return " ".join(words)


def _salutation(hour: int) -> str:
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 18:
        return "Good afternoon"
    return "Good evening"


_GREETINGS = {
    "hi", "hello", "hey", "hiya", "yo", "greetings", "howdy",
    "good morning", "morning", "good afternoon", "afternoon",
    "good evening", "evening", "good day", "hey there", "hello there",
}

_HOW_ARE_YOU = {
    "how are you", "how are you doing", "how are you today",
    "how are you doing today", "how are you doing this evening",
    "how is it going", "how s it going", "how are things",
    "how do you feel", "you alright", "are you alright",
    "are you there", "are you awake",
}

_IDENTITY = {
    "who are you", "what are you", "what is your name",
    "what s your name", "introduce yourself",
}

_CAPABILITIES = {
    "what can you do", "what do you do", "what can i ask",
    "how does this work", "help", "help me",
}

_THANKS = {
    "thanks", "thank you", "thank you very much", "cheers",
    "much appreciated", "appreciated", "ta",
}

_FAREWELL = {
    "bye", "goodbye", "good bye", "good night", "goodnight",
    "see you", "farewell", "that s all", "thats all",
}

_ACKNOWLEDGE = {
    "ok", "okay", "cool", "nice", "great", "righto", "very good",
}

_COMPLIMENT = {
    "well done", "good job", "nice work", "you are the best",
    "you re the best", "brilliant",
}


def small_talk_reply(question: str, hour: int | None = None) -> str | None:
    """Return a canned reply for pure small talk, or None for a real question.

    Only short, whole-message matches qualify.  "Hello, what is our pricing
    strategy?" therefore goes to retrieval instead of being mistaken for a
    greeting.
    """
    text = _normalize(question)
    if not text:
        return None
    if len(text.split()) > 6:
        return None

    salutation = _salutation(datetime.now().hour if hour is None else hour)

    if text in _GREETINGS:
        if text in {"good morning", "morning"}:
            salutation = "Good morning"
        elif text in {"good afternoon", "afternoon"}:
            salutation = "Good afternoon"
        elif text in {"good evening", "evening"}:
            salutation = "Good evening"
        elif text == "good day":
            salutation = "Good day"
        return (
            f"{salutation}, sir. The notes are indexed and the galaxy is stable. "
            "Ask whenever you are ready."
        )
    if text in _HOW_ARE_YOU:
        return "All systems nominal, sir. Rather better than the coffee machine."
    if text in _IDENTITY:
        return "A.S.H.R.A, sir. A note-grounded assistant with a fondness for order."
    if text in _CAPABILITIES:
        return (
            "I can answer from your notes, read the answer aloud, and fly you to "
            "the source. Ask me anything in the vault."
        )
    if text in _THANKS:
        return "At your service, sir."
    if text in _FAREWELL:
        return "Very good, sir. I shall keep the galaxy warm."
    if text in _ACKNOWLEDGE:
        return "Very good, sir."
    if text in _COMPLIMENT:
        return "I do try, sir. The notes do most of the heavy lifting."
    return None

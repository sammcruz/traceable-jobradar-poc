"""Privacy by design at ingest: job ads are about jobs, not about the people who post them.

Recruiter names, e-mail addresses and phone numbers are not useful for matching, so we
remove them before anything is stored. This is data minimisation (GDPR Art. 5(1)(c)).
"""

from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Phone numbers: international prefix, a "Tel:" label, or a German area code (0xx…).
# Deliberately not "any long digit run", so salary ranges like "50.000 - 70.000" survive.
PHONE = re.compile(
    r"(?:(?<![\w+.,])(?:\+|00)\d[\d ()/.-]{7,}\d)"
    r"|(?:\b(?:tel|telefon|phone|mobil|mobile)\b\.?:?\s*\+?\d[\d ()/.-]{5,}\d)"
    r"|(?:(?<![\w.,])0\d{2,4}[ /-]?\d{4,8}\b)",
    re.IGNORECASE,
)
# "Ihr Kontakt: Max Beispiel, …" -> the label and what follows on that line, at most ~80 characters.
CONTACT_LINE = re.compile(
    r"\b(ansprechpartner(?:in)?|ihr kontakt|ihre kontaktperson|kontaktperson|contact person|your contact)\b"
    r"[^\n]{0,80}",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(raw)
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def redact(text: str) -> tuple[str, int]:
    """Remove contact lines, e-mails and phone numbers. Returns (clean text, number of redactions)."""
    count = 0
    for pattern, token in ((CONTACT_LINE, "[contact removed]"), (EMAIL, "[email]"), (PHONE, "[phone]")):
        text, n = pattern.subn(token, text)
        count += n
    return text, count


PERSONAL_KEYS = ("ansprechpartner", "kontakt", "contact", "telefon", "phone", "email", "e-mail",
                 "mail", "fax", "adresse", "address", "anrede", "vorname", "nachname", "recruiter")


def scrub_payload(obj):
    """Raw API payloads get the same treatment before they are stored: personal keys dropped,
    free text redacted. Even the bronze layer is not a place for personal data we don't need."""
    if isinstance(obj, dict):
        return {k: scrub_payload(v) for k, v in obj.items()
                if not any(p in k.lower() for p in PERSONAL_KEYS)}
    if isinstance(obj, list):
        return [scrub_payload(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)[0]
    return obj


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

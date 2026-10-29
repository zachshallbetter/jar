#!/usr/bin/env python3
"""Personal identifiers that never need storing, and money that defaults to the adults (09-30 review).

  redact_personal(text)  one-time codes, meeting passcodes, password values, and account, card,
                         routing and social security numbers, and a device's serial number or IMEI become "[removed]". Kept: the last four
                         digits ("card ending in 2311"), amounts, dates, phone numbers. Used on synced
                         mail before it's indexed (connectors_runner) and on the chat history titan
                         keeps (09-30: a bill PDF's account number sat in it, and every later turn that
                         read that history sent it to Gemini).
  about_money(text)      whether something concerns money: a timeline entry or note titan would share
                         with the household goes to the adults instead (09-30: a phone bill reached the
                         kids' audience twice in one evening).
"""
from __future__ import annotations

import re

_RULES = [
    (re.compile(r"(?i)(\b(?:verification|security|one[- ]time|login|sign[- ]in|confirmation|access)\s+code\b[^0-9\n]{0,30})\b\d{4,8}\b"),
     r"\1[removed]"),
    (re.compile(r"(?i)(\b(?:password|passcode|passwd|pin)\s*[:=]\s*)\S{3,}"), r"\1[removed]"),
    (re.compile(r"(?i)(\b(?:account|acct)\.?(?:\s*(?:number|no\.?|#))?\s*[:#]?\s*)\d[\d\- ]{4,}\d"), r"\1[removed]"),
    (re.compile(r"(?i)(\brouting(?:\s*(?:number|no\.?|#))?\s*[:#]?\s*)\d{9}\b"), r"\1[removed]"),
    (re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b"), "[card number removed]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[removed]"),
    # a device's serial number, IMEI or MEID (10-05: a carrier's trade-in mail put a watch's IMEI in a reply)
    (re.compile(r"(?i)(\b(?:serial(?:\s*(?:number|no\.?|#))?|s/?n|imei|meid)\s*[:#]?\s*`?)[A-Z0-9][A-Z0-9-]{6,22}[A-Z0-9]\b"),
     r"\1[removed]"),
    (re.compile(r"(?<![\d.-])\d{15}(?![\d.-])"), "[device id removed]"),   # a bare 15 digits: an IMEI's shape
]
MONEY_RX = re.compile(
    r"\$\s?\d|\b\d+\.\d{2}\s?(?:usd|dollars)\b|\b(?:bills?|invoices?|payments?|paid|owed?|owes|balances?|bank|"
    r"credit cards?|debit|loans?|mortgage|rent|salary|paychecks?|tax(?:es)?|subscriptions?|autopay|budget|"
    r"spending|refund)\b", re.I)


def redact_personal(text: str) -> str:
    out = str(text or "")
    for rx, rep in _RULES:
        out = rx.sub(rep, out)
    return out


def about_money(text: str) -> bool:
    return bool(MONEY_RX.search(str(text or "")))

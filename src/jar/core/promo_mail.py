#!/usr/bin/env python3
"""Bulk and promotional mail, told apart in code (titan-node#650). From the 10-02 logs audit: a world proposal
("Google's phone is…") came from a Google Ads email, and a mail suggestion from a VALORANT crypto promo. Neither a
marketing mail's phone number nor its "event" is the household's.

A mail is promotional when any of these hold:
  - its headers say so: List-Unsubscribe, or Precedence bulk/list/junk (when the Mac's excerpt carries them);
  - its sender is a marketing address: a local part like news@, offers@, marketing@, ads-noreply@, or a sending
    domain that is a mail service's or a marketing subdomain (email.brand.com, e.brand.com, mailchimp, sendgrid…);
  - its body has a newsletter's footer: "unsubscribe", "view in browser", "manage your preferences", "you're
    receiving this email because…".
A booking confirmation from a no-reply address isn't promotional by its sender alone.

  promo_mail.is_promotional(text)       any sign of bulk or marketing mail (world notes)
  promo_mail.is_marketing(text)         a marketing sender, or a list/footer with selling words (mail suggestions)
  either takes headers={...} when they're given apart from the excerpt
"""
from __future__ import annotations

import re
from typing import Any

_HEADER_RX = re.compile(r"(?m)^([A-Za-z-]+):\s?(.*)$")
# Not info@, hello@, team@ or events@: a school or a club sends real news from those.
_LOCAL_RX = re.compile(r"^(?:news(?:letters?)?|offers?|deals?|promo(?:tions?)?|marketing|mktg|mkt|ads?|"
                       r"ads[-_.]?no-?reply|advertis\w*|campaigns?|specials?|sale|digest|mailer|mailers|bulk)"
                       r"(?:[-_.+][a-z0-9]*)?@", re.I)
_DOMAIN_RX = re.compile(r"@(?:[a-z0-9-]+\.)*(?:mailchimp(?:app)?\.com|mcsv\.net|rsgsv\.net|list-manage\.com|"
                        r"sendgrid\.(?:net|com)|sailthru\.com|klaviyo(?:mail)?\.com|braze\.com|mktomail\.com|"
                        r"marketo\.(?:com|org)|hubspot(?:email)?\.(?:com|net)|hs-?email\.(?:com|net)|"
                        r"exacttarget\.com|cmail\d*\.com|createsend\d*\.com|constantcontact\.com|ccsend\.com|"
                        r"customer\.io|iterable\.com|sparkpost(?:mail)?\.com|mandrillapp\.com|amazonses\.com|"
                        r"bounce\.[a-z0-9.-]+|substack\.com|beehiiv\.com|convertkit\.com)\b|"
                        r"@(?:e|em|email|emails|mail|mailer|mailing|news|newsletter|marketing|mkt|promo|offers|"
                        r"deals|info|engage|reply|go|click|links?)\d*\.[a-z0-9-]+\.[a-z.]{2,}\b", re.I)
_FOOTER_RX = re.compile(r"\bunsubscribe\b|\bview (?:this email |it )?(?:in|on) (?:your |a )?(?:web )?browser\b|"
                        r"\bmanage (?:your )?(?:email |subscription |communication )?preferences\b|"
                        r"\bupdate your (?:email )?preferences\b|\bopt[ -]out\b|"
                        r"\byou(?:'re| are) receiving this (?:e-?mail|message|newsletter)\b|"
                        r"\bthis (?:e-?mail|message) was sent to\b|\bno longer wish to receive\b|"
                        r"\bemail preferences\b|\bprivacy policy\s*\|", re.I)


def headers_of(text: str) -> dict[str, str]:
    """The excerpt's header lines (From:, Subject:, List-Unsubscribe:…) before the first blank line."""
    head = re.split(r"\n\s*\n", text or "", 1)[0][:4000]
    return {m.group(1).lower(): m.group(2).strip() for m in _HEADER_RX.finditer(head)}


def why(text: str, headers: dict[str, Any] | None = None) -> str | None:
    """Why this mail is promotional ("list-unsubscribe", "precedence", "sender", "footer"), or None."""
    h = {str(k).lower(): str(v or "") for k, v in (headers if headers is not None else headers_of(text)).items()}
    if h.get("list-unsubscribe") or h.get("list-id"):
        return "list-unsubscribe"
    if re.match(r"\s*(?:bulk|list|junk)\b", h.get("precedence", ""), re.I):
        return "precedence"
    sender = h.get("from", "")
    addr = (re.search(r"<([^>]+)>", sender) or re.search(r"(\S+@\S+)", sender))
    addr = addr.group(1).strip().lower() if addr else ""
    if addr and (_LOCAL_RX.match(addr) or _DOMAIN_RX.search(addr)):
        return "sender"
    if _FOOTER_RX.search(text or ""):
        return "footer"
    return None


_SELLING_RX = re.compile(r"\b(?:\d+\s?% off|sale|deals?|offer|promo(?:tion)?|coupon|discount|shop now|buy now|order now|"
                         r"limited[- ]time|exclusive|free shipping|crypto|bitcoin|nft|giveaway|sweepstakes|win (?:a|up)|"
                         r"reward points|upgrade now|subscribe now|ads? (?:account|credit|campaign)|bonus)\b", re.I)


def is_promotional(text: str, headers: dict[str, Any] | None = None) -> bool:
    """Any sign of bulk or marketing mail (world notes: a list's phone or address is never a household fact)."""
    return why(text, headers) is not None


def is_marketing(text: str, headers: dict[str, Any] | None = None) -> bool:
    """Marketing, not just a list (mail suggestions: a school's newsletter about a recital is still an event). A
    marketing sender is enough; a list header or a newsletter footer only with selling words."""
    w = why(text, headers)
    return w == "sender" or (w is not None and bool(_SELLING_RX.search(text or "")))

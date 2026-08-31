from __future__ import annotations

import re

_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: str) -> str | None:
    digits = _DIGITS.sub("", raw or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if digits.startswith("7") and len(digits) == 11:
        return f"+{digits}"
    return None


def format_phone(phone: str) -> str:
    digits = _DIGITS.sub("", phone or "")
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return phone

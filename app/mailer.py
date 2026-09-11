from __future__ import annotations

import imaplib
import logging
import re
import smtplib
import time
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from app.config import Settings

log = logging.getLogger(__name__)

_SENT_FOLDER_CANDIDATES = (
    "Отправленные",
    "Sent",
    "Sent Items",
    "Sent Messages",
    "INBOX.Sent",
    "INBOX.Отправленные",
)


def smtp_configured(settings: Settings) -> bool:
    return bool(settings.smtp_user and settings.smtp_password and settings.smtp_from and settings.order_email_to)


def smtp_ready(settings: Settings) -> bool:
    return bool(settings.smtp_user and settings.smtp_password and settings.smtp_from)


def send_order_email(
    settings: Settings,
    subject: str,
    body: str,
    xls_bytes: bytes,
    filename: str,
    *,
    cc: str | None = None,
) -> None:
    if not settings.smtp_user or not settings.smtp_password:
        raise RuntimeError("В .env не заданы SMTP_USER / SMTP_PASSWORD (или IMAP_PASSWORD)")

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = settings.order_email_to
    if cc and cc.strip():
        msg["Cc"] = cc.strip()
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")
    msg.add_attachment(
        xls_bytes,
        maintype="application",
        subtype="vnd.ms-excel",
        filename=filename,
    )
    _send(settings, msg)


def send_text_email(settings: Settings, *, to: str, subject: str, body: str) -> None:
    if not smtp_ready(settings):
        raise RuntimeError("В .env не заданы SMTP_USER / SMTP_PASSWORD / SMTP_FROM")
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")
    _send(settings, msg)


def _message_recipients(msg: EmailMessage) -> list[str]:
    addrs: list[str] = []
    for header in ("To", "Cc", "Bcc"):
        if header in msg:
            addrs.extend(addr for _, addr in getaddresses(msg.get_all(header, [])) if addr)
    # уникальные, порядок сохраняем
    seen: set[str] = set()
    result: list[str] = []
    for addr in addrs:
        key = addr.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(addr)
    return result


def _prepare_raw(msg: EmailMessage) -> tuple[str, list[str], bytes]:
    """Один раз сериализуем письмо — эти же байты уйдут в SMTP и в «Отправленные»."""
    if "Date" not in msg:
        msg["Date"] = formatdate(localtime=True)
    if "Message-ID" not in msg:
        msg["Message-ID"] = make_msgid()
    from_addr = getaddresses([msg["From"] or ""])[0][1] if msg["From"] else ""
    if not from_addr:
        raise RuntimeError("В письме нет From")
    recipients = _message_recipients(msg)
    if not recipients:
        raise RuntimeError("В письме нет получателей")
    if "Bcc" in msg:
        del msg["Bcc"]
    raw = msg.as_bytes(policy=policy.SMTP)
    return from_addr, recipients, raw


def _send(settings: Settings, msg: EmailMessage) -> None:
    from_addr, recipients, raw = _prepare_raw(msg)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(from_addr, recipients, raw)
    try:
        _save_to_sent(settings, raw)
    except Exception:
        log.exception("Письмо ушло, но в «Отправленные» через IMAP не сохранилось")


def _imap_login(settings: Settings) -> imaplib.IMAP4_SSL:
    if not settings.imap_host or not settings.imap_user or not settings.imap_password:
        raise RuntimeError("IMAP не настроен (IMAP_HOST / IMAP_USER / IMAP_PASSWORD)")
    client = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=30)
    client.login(settings.imap_user, settings.imap_password)
    return client


def _decode_mailbox_name(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1", errors="replace")
    return str(raw)


def _decode_imap_utf7(name: str) -> str:
    """IMAP modified UTF-7 → unicode (для &BB4EQgQ... = Отправленные)."""
    if "&" not in name:
        return name
    try:
        return name.encode("ascii").decode("imap4-utf-7")
    except Exception:
        try:
            import base64

            out: list[str] = []
            i = 0
            while i < len(name):
                if name[i] != "&":
                    out.append(name[i])
                    i += 1
                    continue
                j = name.find("-", i)
                if j < 0:
                    out.append(name[i:])
                    break
                chunk = name[i + 1 : j]
                i = j + 1
                if chunk == "":
                    out.append("&")
                    continue
                pad = (-len(chunk)) % 4
                decoded = base64.b64decode(chunk.replace(",", "/") + "=" * pad)
                out.append(decoded.decode("utf-16-be", errors="replace"))
            return "".join(out)
        except Exception:
            return name


def _parse_list_line(line: str) -> tuple[str, str]:
    """Возвращает (flags_lower, mailbox_name)."""
    flags = ""
    m = re.match(r"\(([^)]*)\)", line)
    if m:
        flags = m.group(1).casefold()
    quoted = re.findall(r'"([^"]*)"', line)
    if quoted:
        name = quoted[-1]
    else:
        name = line.split()[-1] if line.split() else line
    return flags, name


def _list_mailboxes(client: imaplib.IMAP4_SSL) -> list[tuple[str, str]]:
    """Список (flags, name) как на сервере."""
    typ, data = client.list()
    if typ != "OK" or not data:
        return []
    result: list[tuple[str, str]] = []
    for item in data:
        if not item:
            continue
        line = _decode_mailbox_name(item)
        result.append(_parse_list_line(line))
    return result


def _pick_sent_folder(client: imaplib.IMAP4_SSL) -> str:
    boxes = _list_mailboxes(client)
    for flags, name in boxes:
        if "\\sent" in flags.split():
            return name
    by_decoded = {_decode_imap_utf7(name).casefold(): name for _, name in boxes}
    for candidate in _SENT_FOLDER_CANDIDATES:
        if candidate.casefold() in by_decoded:
            return by_decoded[candidate.casefold()]
    for _, name in boxes:
        decoded = _decode_imap_utf7(name).casefold()
        if "sent" in decoded or "отправлен" in decoded:
            return name
    raise RuntimeError("Не нашёл папку «Отправленные» на IMAP")


def _save_to_sent(settings: Settings, raw: bytes) -> None:
    if not settings.imap_host or not settings.imap_user or not settings.imap_password:
        log.info("IMAP не задан — копию в «Отправленные» не кладу")
        return
    client = _imap_login(settings)
    try:
        folder = _pick_sent_folder(client)
        typ, resp = client.append(folder, "\\Seen", imaplib.Time2Internaldate(time.time()), raw)
        if typ != "OK":
            raise RuntimeError(f"IMAP APPEND в {folder!r} вернул {typ}: {resp}")
        log.info(
            "Копия письма сохранена в IMAP-папку %s (%s)",
            folder,
            _decode_imap_utf7(folder),
        )
    finally:
        try:
            client.logout()
        except Exception:
            pass

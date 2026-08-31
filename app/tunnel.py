from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from asyncio.subprocess import PIPE, STDOUT
from pathlib import Path

log = logging.getLogger(__name__)
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_WINDOWS_CANDIDATES = (
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "cloudflared" / "cloudflared.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "cloudflared" / "cloudflared.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "cloudflared" / "cloudflared.exe",
)


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared") or shutil.which("cloudflared.exe")
    if found:
        return found
    for path in _WINDOWS_CANDIDATES:
        if path and path.is_file():
            return str(path)
    return None


async def start_cloudflared(port: int) -> tuple[asyncio.subprocess.Process | None, str | None]:
    binary = find_cloudflared()
    if not binary:
        log.warning("cloudflared не найден в PATH")
        return None, None
    log.info("cloudflared: %s", binary)
    proc = await asyncio.create_subprocess_exec(
        binary,
        "tunnel",
        "--no-autoupdate",
        "--url",
        f"http://127.0.0.1:{port}",
        stdout=PIPE,
        stderr=STDOUT,
    )
    url = None
    deadline = asyncio.get_event_loop().time() + 40
    while asyncio.get_event_loop().time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        except asyncio.TimeoutError:
            continue
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if text:
            log.info("cloudflared: %s", text)
        match = URL_RE.search(text)
        if match:
            url = match.group(0)
            break
    return proc, url

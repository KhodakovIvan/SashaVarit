from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.storage import Storage
from app.webapp_server import MenuCache


@dataclass
class Ctx:
    settings: Settings
    storage: Storage
    cache: MenuCache
    public_url: str = ""

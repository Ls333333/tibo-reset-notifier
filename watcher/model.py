from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Post:
    key: str
    source: str
    title: str
    body: str
    url: str
    created_at: str
    is_reply: bool = False

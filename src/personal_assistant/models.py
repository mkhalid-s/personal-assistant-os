from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ExternalItem:
    connector: str
    external_id: str
    item_type: str
    title: str
    body: str
    owner: str | None = None
    status: str | None = None
    priority: str | None = None
    due_date: str | None = None
    url: str | None = None
    raw: dict[str, Any] | None = None

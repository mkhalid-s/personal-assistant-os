from __future__ import annotations

import os

from personal_assistant.models import ExternalItem

from .base import BaseConnector


class AhaConnector(BaseConnector):
    name = "aha"

    def required_env(self) -> list[str]:
        return ["AHA_BASE_URL", "AHA_API_TOKEN"]

    def fetch_items(self) -> list[ExternalItem]:
        base = os.environ["AHA_BASE_URL"].rstrip("/")
        token = os.environ["AHA_API_TOKEN"]
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        data = self.json_get(f"{base}/api/v1/features?per_page=30", headers)
        items: list[ExternalItem] = []
        for f in data.get("features", []):
            items.append(
                ExternalItem(
                    connector=self.name,
                    external_id=str(f.get("id", "")),
                    item_type="feature",
                    title=f.get("name", ""),
                    body=f.get("description") or "",
                    owner=(f.get("assigned_to_user") or {}).get("name"),
                    status=(f.get("workflow_status") or {}).get("name"),
                    due_date=f.get("due_date"),
                    url=f.get("url"),
                    raw=f,
                )
            )
        return items

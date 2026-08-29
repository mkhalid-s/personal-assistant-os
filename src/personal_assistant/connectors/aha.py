from __future__ import annotations

import os

from personal_assistant.models import ExternalItem

from .base import BaseConnector


class AhaConnector(BaseConnector):
    name = "aha"

    def required_env(self) -> list[str]:
        # AHA_API_KEY matches the env var used by the write adapter in execution.py.
        return ["AHA_BASE_URL", "AHA_API_KEY"]

    def fetch_items(self) -> list[ExternalItem]:
        base = os.environ["AHA_BASE_URL"].rstrip("/")
        token = os.environ["AHA_API_KEY"]
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        # json_get_paged handles dict response with result_key="features".
        features = self.json_get_paged(
            f"{base}/api/v1/features",
            headers,
            result_key="features",
            page_param="page",
            size_param="per_page",
        )
        items: list[ExternalItem] = []
        for f in features:
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

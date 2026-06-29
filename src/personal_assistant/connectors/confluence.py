from __future__ import annotations

import os
import urllib.parse

from personal_assistant.models import ExternalItem
from .base import BaseConnector


class ConfluenceConnector(BaseConnector):
    name = "confluence"

    def required_env(self) -> list[str]:
        return ["CONFLUENCE_BASE_URL", "CONFLUENCE_USER_EMAIL", "CONFLUENCE_API_TOKEN"]

    def fetch_items(self) -> list[ExternalItem]:
        base = os.environ["CONFLUENCE_BASE_URL"].rstrip("/")
        email = os.environ["CONFLUENCE_USER_EMAIL"]
        token = os.environ["CONFLUENCE_API_TOKEN"]
        import base64
        auth = (f"{email}:{token}").encode("utf-8")
        cql = urllib.parse.quote('type=page order by lastmodified desc')
        url = f"{base}/wiki/rest/api/search?cql={cql}&limit=20"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {base64.b64encode(auth).decode('utf-8')}",
        }
        data = self.json_get(url, headers)
        items: list[ExternalItem] = []
        for row in data.get("results", []):
            content = row.get("content") or {}
            item_id = str(content.get("id", ""))
            title = content.get("title", "")
            links = content.get("_links") or {}
            webui = links.get("webui", "")
            items.append(
                ExternalItem(
                    connector=self.name,
                    external_id=item_id,
                    item_type="page",
                    title=title,
                    body="",
                    owner=None,
                    status="active",
                    url=f"{base}{webui}" if webui else None,
                    raw=row,
                )
            )
        return items

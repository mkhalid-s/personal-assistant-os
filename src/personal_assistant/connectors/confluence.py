from __future__ import annotations

import base64
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
        auth = f"{email}:{token}".encode()
        cql = urllib.parse.quote("type=page order by lastmodified desc")
        # No limit param here — json_get_linked follows _links.next automatically.
        url = f"{base}/wiki/rest/api/search?cql={cql}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {base64.b64encode(auth).decode('utf-8')}",
        }
        rows = self.json_get_linked(
            url,
            headers,
            result_key="results",
            next_path="_links.next",
            base_url=base,
        )
        items: list[ExternalItem] = []
        for row in rows:
            content = row.get("content") or {}
            item_id = str(content.get("id", ""))
            title = content.get("title", "")
            links = content.get("_links") or {}
            webui = links.get("webui", "")
            # Fetch page body if available in the search excerpt.
            excerpt = row.get("excerpt", "")
            items.append(
                ExternalItem(
                    connector=self.name,
                    external_id=item_id,
                    item_type="page",
                    title=title,
                    body=excerpt,
                    owner=None,
                    status="active",
                    url=f"{base}{webui}" if webui else None,
                    raw=row,
                )
            )
        return items

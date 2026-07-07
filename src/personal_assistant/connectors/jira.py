from __future__ import annotations

import os
import urllib.parse

from personal_assistant.models import ExternalItem
from .base import BaseConnector


class JiraConnector(BaseConnector):
    name = "jira"

    def required_env(self) -> list[str]:
        return ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"]

    def fetch_items(self) -> list[ExternalItem]:
        base = os.environ["JIRA_BASE_URL"].rstrip("/")
        email = os.environ["JIRA_USER_EMAIL"]
        token = os.environ["JIRA_API_TOKEN"]
        jql = urllib.parse.quote("assignee = currentUser() ORDER BY updated DESC")
        url = f"{base}/rest/api/3/search?jql={jql}&maxResults=30"
        auth = (f"{email}:{token}").encode("utf-8")
        import base64

        headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {base64.b64encode(auth).decode('utf-8')}",
        }
        data = self.json_get(url, headers)
        items: list[ExternalItem] = []
        for issue in data.get("issues", []):
            fields = issue.get("fields", {})
            assignee = fields.get("assignee") or {}
            status = (fields.get("status") or {}).get("name")
            priority = (fields.get("priority") or {}).get("name")
            due_date = fields.get("duedate")
            items.append(
                ExternalItem(
                    connector=self.name,
                    external_id=issue.get("id", ""),
                    item_type="issue",
                    title=issue.get("key", "") + ": " + (fields.get("summary") or ""),
                    body=(fields.get("description") or "") if isinstance(fields.get("description"), str) else "",
                    owner=assignee.get("displayName"),
                    status=status,
                    priority=priority,
                    due_date=due_date,
                    url=f"{base}/browse/{issue.get('key', '')}",
                    raw=issue,
                )
            )
        return items

from __future__ import annotations

import os

from personal_assistant.models import ExternalItem
from .base import BaseConnector


class GitHubConnector(BaseConnector):
    name = "github"

    def required_env(self) -> list[str]:
        return ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"]

    def fetch_items(self) -> list[ExternalItem]:
        token = os.environ["GITHUB_TOKEN"]
        owner = os.environ["GITHUB_OWNER"]
        repo = os.environ["GITHUB_REPO"]
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        prs = self.json_get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=30",
            headers,
        )
        items: list[ExternalItem] = []
        for pr in prs:
            items.append(
                ExternalItem(
                    connector=self.name,
                    external_id=str(pr.get("id", "")),
                    item_type="pull_request",
                    title=pr.get("title", ""),
                    body=pr.get("body") or "",
                    owner=(pr.get("user") or {}).get("login"),
                    status=pr.get("state"),
                    due_date=None,
                    url=pr.get("html_url"),
                    raw=pr,
                )
            )
        return items

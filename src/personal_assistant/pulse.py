from __future__ import annotations

import time
from datetime import datetime

from personal_assistant.connectors import AhaConnector, ConfluenceConnector, GitHubConnector, JiraConnector
from personal_assistant.db import get_connection


def detect_mode(meeting_hours: float) -> str:
    if meeting_hours >= 5:
        return "meeting-heavy"
    if meeting_hours >= 2.5:
        return "hybrid"
    return "maker"


def run_cycle(meeting_hours: float = 0.0) -> list[str]:
    conn = get_connection()
    outputs: list[str] = []
    for connector_cls in [JiraConnector, GitHubConnector, ConfluenceConnector, AhaConnector]:
        res = connector_cls(conn).sync()
        outputs.append(f"{res.connector}:{res.status}:{res.fetched}")

    mode = detect_mode(meeting_hours)
    conn.execute(
        "INSERT INTO daily_logs (summary, mode, note) VALUES (?, ?, ?)",
        (
            f"Pulse cycle completed at {datetime.now().isoformat(timespec='minutes')}",
            mode,
            "automated pulse",
        ),
    )
    conn.commit()
    return outputs


def run_forever(interval_sec: int = 1800, meeting_hours: float = 0.0) -> None:
    while True:
        run_cycle(meeting_hours=meeting_hours)
        time.sleep(interval_sec)

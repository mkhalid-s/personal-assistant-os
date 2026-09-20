"""Test connector failure scenarios.

Tests cover network errors, authentication failures, rate limits,
malformed responses, and missing configuration for all connectors.
"""

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from personal_assistant.connectors.aha import AhaConnector
from personal_assistant.connectors.confluence import ConfluenceConnector
from personal_assistant.connectors.github import GitHubConnector
from personal_assistant.connectors.jira import JiraConnector
from personal_assistant.db import initialize_schema


class JiraConnectorFailureTest(unittest.TestCase):
    """Test Jira connector failure scenarios."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        initialize_schema(self.conn)
        self.connector = JiraConnector(self.conn)
        self.addCleanup(self.conn.close)

    def test_missing_env_vars(self):
        """Test Jira connector with missing environment variables."""
        with patch.dict("os.environ", {}, clear=True):
            result = self.connector.sync()
            self.assertEqual(result.status, "skipped")
            self.assertIn("missing env var", result.message)

    def test_partial_env_vars(self):
        """Test Jira connector with only some environment variables."""
        with patch.dict(
            "os.environ",
            {"JIRA_BASE_URL": "https://example.atlassian.net", "JIRA_USER_EMAIL": "test@example.com"},
            clear=True,
        ):
            result = self.connector.sync()
            self.assertEqual(result.status, "skipped")
            self.assertIn("missing env var", result.message)

    def test_auth_failure(self):
        """Test Jira connector with invalid credentials."""
        with (
            patch.dict(
                "os.environ",
                {
                    "JIRA_BASE_URL": "https://example.atlassian.net",
                    "JIRA_USER_EMAIL": "test@example.com",
                    "JIRA_API_TOKEN": "invalid_token",
                },
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.atlassian.net", 401, "Unauthorized", {}, None
            )
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("401", result.message)

    def test_network_timeout(self):
        """Test Jira connector behavior on network timeout."""
        with (
            patch.dict(
                "os.environ",
                {
                    "JIRA_BASE_URL": "https://example.atlassian.net",
                    "JIRA_USER_EMAIL": "test@example.com",
                    "JIRA_API_TOKEN": "token",
                },
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = TimeoutError("Connection timed out")
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("timed out", result.message.lower())

    def test_malformed_json_response(self):
        """Test Jira connector with malformed JSON response."""
        with (
            patch.dict(
                "os.environ",
                {
                    "JIRA_BASE_URL": "https://example.atlassian.net",
                    "JIRA_USER_EMAIL": "test@example.com",
                    "JIRA_API_TOKEN": "token",
                },
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            mock_response = MagicMock()
            mock_response.read.return_value = b"invalid json"
            mock_urlopen.return_value.__enter__.return_value = mock_response
            result = self.connector.sync()
            self.assertEqual(result.status, "error")

    def test_rate_limit_handling(self):
        """Test Jira connector rate limit (429) handling."""
        with (
            patch.dict(
                "os.environ",
                {
                    "JIRA_BASE_URL": "https://example.atlassian.net",
                    "JIRA_USER_EMAIL": "test@example.com",
                    "JIRA_API_TOKEN": "token",
                },
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.atlassian.net", 429, "Too Many Requests", {}, None
            )
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("429", result.message)


class GitHubConnectorFailureTest(unittest.TestCase):
    """Test GitHub connector failure scenarios."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        initialize_schema(self.conn)
        self.connector = GitHubConnector(self.conn)
        self.addCleanup(self.conn.close)

    def test_missing_env_vars(self):
        """Test GitHub connector with missing environment variables."""
        with patch.dict("os.environ", {}, clear=True):
            result = self.connector.sync()
            self.assertEqual(result.status, "skipped")
            self.assertIn("missing env var", result.message)

    def test_invalid_token(self):
        """Test GitHub connector with invalid token."""
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "invalid_token", "GITHUB_OWNER": "test", "GITHUB_REPO": "test"}),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError("https://api.github.com", 401, "Unauthorized", {}, None)
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("401", result.message)

    def test_repo_not_found(self):
        """Test GitHub connector with non-existent repository."""
        with (
            patch.dict(
                "os.environ", {"GITHUB_TOKEN": "token", "GITHUB_OWNER": "nonexistent", "GITHUB_REPO": "nonexistent"}
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError("https://api.github.com", 404, "Not Found", {}, None)
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("404", result.message)

    def test_network_error(self):
        """Test GitHub connector behavior on network errors."""
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "token", "GITHUB_OWNER": "test", "GITHUB_REPO": "test"}),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.URLError("Network unreachable")
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("Network unreachable", result.message)

    def test_empty_response(self):
        """Test GitHub connector with empty response list."""
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "token", "GITHUB_OWNER": "test", "GITHUB_REPO": "test"}),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            mock_response = MagicMock()
            mock_response.read.return_value = b"[]"
            mock_urlopen.return_value.__enter__.return_value = mock_response
            result = self.connector.sync()
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.fetched, 0)


class ConfluenceConnectorFailureTest(unittest.TestCase):
    """Test Confluence connector failure scenarios."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        initialize_schema(self.conn)
        self.connector = ConfluenceConnector(self.conn)
        self.addCleanup(self.conn.close)

    def test_missing_env_vars(self):
        """Test Confluence connector with missing environment variables."""
        with patch.dict("os.environ", {}, clear=True):
            result = self.connector.sync()
            self.assertEqual(result.status, "skipped")
            self.assertIn("missing env var", result.message)

    def test_invalid_credentials(self):
        """Test Confluence connector with invalid credentials."""
        with (
            patch.dict(
                "os.environ",
                {
                    "CONFLUENCE_BASE_URL": "https://example.atlassian.net/wiki",
                    "CONFLUENCE_USER_EMAIL": "test@example.com",
                    "CONFLUENCE_API_TOKEN": "invalid_token",
                },
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.atlassian.net", 401, "Unauthorized", {}, None
            )
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("401", result.message)

    def test_space_not_found(self):
        """Test Confluence connector when space doesn't exist."""
        with (
            patch.dict(
                "os.environ",
                {
                    "CONFLUENCE_BASE_URL": "https://example.atlassian.net/wiki",
                    "CONFLUENCE_USER_EMAIL": "test@example.com",
                    "CONFLUENCE_API_TOKEN": "token",
                },
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.atlassian.net", 404, "Not Found", {}, None
            )
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("404", result.message)

    def test_permission_denied(self):
        """Test Confluence connector with insufficient permissions."""
        with (
            patch.dict(
                "os.environ",
                {
                    "CONFLUENCE_BASE_URL": "https://example.atlassian.net/wiki",
                    "CONFLUENCE_USER_EMAIL": "test@example.com",
                    "CONFLUENCE_API_TOKEN": "token",
                },
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.atlassian.net", 403, "Forbidden", {}, None
            )
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("403", result.message)


class AhaConnectorFailureTest(unittest.TestCase):
    """Test Aha connector failure scenarios."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        initialize_schema(self.conn)
        self.connector = AhaConnector(self.conn)
        self.addCleanup(self.conn.close)

    def test_missing_env_vars(self):
        """Test Aha connector with missing environment variables."""
        with patch.dict("os.environ", {}, clear=True):
            result = self.connector.sync()
            self.assertEqual(result.status, "skipped")
            self.assertIn("missing env var", result.message)

    def test_invalid_api_key(self):
        """Test Aha connector with invalid API key."""
        with (
            patch.dict("os.environ", {"AHA_BASE_URL": "https://example.aha.io", "AHA_API_KEY": "invalid_key"}),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError("https://example.aha.io", 401, "Unauthorized", {}, None)
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("401", result.message)

    def test_feature_not_found(self):
        """Test Aha connector with non-existent feature."""
        with (
            patch.dict("os.environ", {"AHA_BASE_URL": "https://example.aha.io", "AHA_API_KEY": "key"}),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError("https://example.aha.io", 404, "Not Found", {}, None)
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("404", result.message)

    def test_account_disabled(self):
        """Test Aha connector when account is disabled."""
        with (
            patch.dict("os.environ", {"AHA_BASE_URL": "https://example.aha.io", "AHA_API_KEY": "key"}),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.aha.io", 403, "Account disabled", {}, None
            )
            result = self.connector.sync()
            self.assertEqual(result.status, "error")
            self.assertIn("403", result.message)


class GenericConnectorFailureHandlingTest(unittest.TestCase):
    """Test generic connector failure handling patterns."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        initialize_schema(self.conn)
        self.addCleanup(self.conn.close)

    def test_sync_state_error_on_failure(self):
        """Test that sync_state is updated with error status on failure."""
        connector = JiraConnector(self.conn)
        with (
            patch.dict(
                "os.environ",
                {
                    "JIRA_BASE_URL": "https://example.atlassian.net",
                    "JIRA_USER_EMAIL": "test@example.com",
                    "JIRA_API_TOKEN": "invalid_token",
                },
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.atlassian.net", 401, "Unauthorized", {}, None
            )
            result = connector.sync()
            self.assertEqual(result.status, "error")

            # Verify sync_state was updated
            row = self.conn.execute(
                "SELECT last_status, last_error FROM sync_state WHERE connector = ?", ("jira",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["last_status"], "error")
            self.assertIn("401", row["last_error"])

    def test_sync_state_skipped_on_missing_env(self):
        """Test that sync_state is updated with skipped status on missing env."""
        connector = GitHubConnector(self.conn)
        with patch.dict("os.environ", {}, clear=True):
            result = connector.sync()
            self.assertEqual(result.status, "skipped")

            # Verify sync_state was updated
            row = self.conn.execute(
                "SELECT last_status, last_error FROM sync_state WHERE connector = ?", ("github",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["last_status"], "skipped")
            self.assertIn("missing env var", row["last_error"])

    def test_sync_state_ok_on_success(self):
        """Test that sync_state is updated with ok status on success."""
        connector = GitHubConnector(self.conn)
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "token", "GITHUB_OWNER": "test", "GITHUB_REPO": "test"}),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            mock_response = MagicMock()
            mock_response.read.return_value = b"[]"
            mock_urlopen.return_value.__enter__.return_value = mock_response
            result = connector.sync()
            self.assertEqual(result.status, "ok")

            # Verify sync_state was updated
            row = self.conn.execute(
                "SELECT last_status, last_error FROM sync_state WHERE connector = ?", ("github",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["last_status"], "ok")
            self.assertEqual(row["last_error"], "")

    def test_retry_logic_on_transient_errors(self):
        """Test that transient errors trigger retry logic."""
        connector = JiraConnector(self.conn)
        with (
            patch.dict(
                "os.environ",
                {
                    "JIRA_BASE_URL": "https://example.atlassian.net",
                    "JIRA_USER_EMAIL": "test@example.com",
                    "JIRA_API_TOKEN": "token",
                },
            ),
            patch("personal_assistant.connectors.base.urllib.request.urlopen") as mock_urlopen,
        ):
            import urllib.error

            # First attempt fails with 503, second succeeds
            mock_urlopen.side_effect = [
                urllib.error.HTTPError("https://example.atlassian.net", 503, "Service Unavailable", {}, None),
                MagicMock(__enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=b'{"issues":[]}')))),
            ]
            result = connector.sync()
            # Should succeed after retry
            self.assertEqual(result.status, "ok")

    def test_no_retry_on_client_errors(self):
        """Test that 4xx client errors do not trigger retry."""
        connector = JiraConnector(self.conn)
        with patch.dict(
            "os.environ",
            {
                "JIRA_BASE_URL": "https://example.atlassian.net",
                "JIRA_USER_EMAIL": "test@example.com",
                "JIRA_API_TOKEN": "token",
            },
        ):
            call_count = [0]

            def mock_urlopen_with_count(*args, **kwargs):
                call_count[0] += 1
                import urllib.error

                raise urllib.error.HTTPError("https://example.atlassian.net", 401, "Unauthorized", {}, None)

            with patch(
                "personal_assistant.connectors.base.urllib.request.urlopen", side_effect=mock_urlopen_with_count
            ):
                result = connector.sync()
                self.assertEqual(result.status, "error")
                # Should only be called once (no retry for 401)
                self.assertEqual(call_count[0], 1)


if __name__ == "__main__":
    unittest.main()

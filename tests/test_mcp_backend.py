"""Tests for the configurable assistant backend.

The MCP connectors are reached through a CLI whose name and tool-id prefix
are configuration rather than constants. Two properties matter and are easy
to regress: unconfigured must mean "skip", never "crash", and a configured
prefix must compose tool ids exactly as the connector expects.
"""

import os
import unittest

from context import U, default_config


class TestAgentConfiguration(unittest.TestCase):
    """agent_cli() and mcp_prefix(): environment wins over the config file."""

    def setUp(self):
        # Neither variable may leak in from the developer's own shell, or the
        # "unconfigured" cases below would silently test nothing.
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("UOA_AGENT_CLI", "UOA_MCP_PREFIX")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_defaults_are_empty(self):
        """Nothing vendor-specific is baked into the source."""
        cfg = default_config()
        self.assertEqual(U.agent_cli(cfg), "")
        self.assertEqual(U.mcp_prefix(cfg), "")

    def test_environment_overrides_config(self):
        os.environ["UOA_AGENT_CLI"] = "some-cli"
        os.environ["UOA_MCP_PREFIX"] = "mcp__vendor_"
        cfg = default_config()
        self.assertEqual(U.agent_cli(cfg), "some-cli")
        self.assertEqual(U.mcp_prefix(cfg), "mcp__vendor_")

    def test_config_is_used_when_environment_is_unset(self):
        cfg = default_config()
        cfg.set("agent", "cli", "from-config")
        cfg.set("agent", "mcp_prefix", "mcp__from_config_")
        self.assertEqual(U.agent_cli(cfg), "from-config")
        self.assertEqual(U.mcp_prefix(cfg), "mcp__from_config_")

    def test_whitespace_is_stripped(self):
        """A value pasted into the ini with a trailing space still works."""
        os.environ["UOA_AGENT_CLI"] = "  spaced-cli  "
        self.assertEqual(U.agent_cli(default_config()), "spaced-cli")


class TestToolIdComposition(unittest.TestCase):
    """mcp_tools(): <prefix><service>__<name>, or nothing at all."""

    def setUp(self):
        self._saved = os.environ.pop("UOA_MCP_PREFIX", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("UOA_MCP_PREFIX", None)
        else:
            os.environ["UOA_MCP_PREFIX"] = self._saved

    def test_composes_full_ids(self):
        os.environ["UOA_MCP_PREFIX"] = "mcp__vendor_"
        self.assertEqual(
            U.mcp_tools("Gmail", ["get_message", "search_threads"]),
            ["mcp__vendor_Gmail__get_message",
             "mcp__vendor_Gmail__search_threads"])

    def test_no_prefix_yields_no_tools(self):
        """An empty list is the signal mcp_ask reads as "no MCP available"."""
        cfg = default_config()
        self.assertEqual(U.mcp_tools("Gmail", ["get_message"], cfg), [])

    def test_write_tools_are_read_tools_plus_labels(self):
        """The write list must stay a superset, and stay narrow."""
        os.environ["UOA_MCP_PREFIX"] = "p_"
        read, write = U.gmail_read_tools(), U.gmail_write_tools()
        self.assertTrue(set(read).issubset(set(write)))
        self.assertEqual(len(write), len(read) + 1)
        self.assertIn("p_Gmail__update_message_labels", write)

    def test_every_connector_composes(self):
        os.environ["UOA_MCP_PREFIX"] = "p_"
        for tools in (U.cal_tools(), U.gmail_read_tools(),
                      U.drive_tools(), U.trello_tools()):
            self.assertTrue(tools)
            for tool in tools:
                self.assertTrue(tool.startswith("p_"))
                self.assertIn("__", tool[2:])


class TestGracefulDegradation(unittest.TestCase):
    """Nothing configured must degrade to a skip, never to an exception."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("UOA_AGENT_CLI", "UOA_MCP_PREFIX")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_mcp_ask_returns_none_without_a_cli(self):
        self.assertIsNone(U.mcp_ask("anything", ["some__tool"], tool="test"))

    def test_mcp_ask_returns_none_without_tools(self):
        os.environ["UOA_AGENT_CLI"] = "definitely-not-a-real-binary"
        self.assertIsNone(U.mcp_ask("anything", [], tool="test"))

    def test_mcp_ask_returns_none_when_cli_is_missing(self):
        os.environ["UOA_AGENT_CLI"] = "definitely-not-a-real-binary"
        os.environ["UOA_MCP_PREFIX"] = "p_"
        self.assertIsNone(U.mcp_ask("anything", U.gmail_read_tools(),
                                    tool="test"))

    def test_mcp_available_is_false_when_unconfigured(self):
        self.assertFalse(U.mcp_available(default_config()))

    def test_no_tool_id_is_hardcoded(self):
        """The whole point of the indirection: no literal tool id in bin/.

        Tool ids are composed from the configured prefix, so a bare "mcp__"
        anywhere under bin/ means someone pasted a vendor's id back in. The
        regression is silent — everything keeps working — so it is asserted
        directly rather than left to review.
        """
        import glob
        import io
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for path in sorted(glob.glob(os.path.join(root, "bin", "*"))):
            if not os.path.isfile(path):
                continue
            with io.open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read().lower()
            self.assertNotIn("mcp__", text, path)


if __name__ == "__main__":
    unittest.main()

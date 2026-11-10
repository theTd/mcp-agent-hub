"""Deterministic native transcript and persistence stress checks; no model calls.

Run: python stress_test_suite.py
Uses isolated temporary data and never connects to an installed Hub.
"""
import unittest

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "test_native_records",
        "test_hub_persistence.HubIntegrationTests.test_dsh_thousand_line_backlog_remains_interactive",
        "test_hub_persistence.HubIntegrationTests.test_http_and_terminal_during_five_second_write_lock",
    ])
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

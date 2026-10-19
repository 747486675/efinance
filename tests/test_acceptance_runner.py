"""Acceptance reporting must not turn empty dependencies into successful samples."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from regression import Checker, finish
from efinance.exceptions import NetworkError, SymbolNotFoundError
from efinance import utils
from unittest.mock import patch


class AcceptanceRunnerTests(unittest.TestCase):
    def test_unknown_symbol_default_and_explicit_suppression_are_distinct(self):
        with patch.object(utils, "search_quote", return_value=None):
            with self.assertRaises(SymbolNotFoundError):
                utils.get_quote_id("unknown", use_local=False)
            self.assertEqual(utils.get_quote_id("unknown", use_local=False, suppress_error=True), "")

    def test_empty_dependency_does_not_provide_a_sample(self):
        checker = Checker()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(checker.rows("market", lambda: pd.DataFrame(columns=["代码"])))
        self.assertEqual(checker.passed, 0)
        self.assertEqual(checker.records[0]["status"], "FAIL")

    def test_failure_skip_and_success_are_preserved_in_report(self):
        checker = Checker()

        def upstream_failure():
            raise NetworkError("fixture unavailable")

        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            checker.rows("upstream", upstream_failure)
            checker.skip("dependent history", "market unavailable")
            checker.rows("available", lambda: pd.DataFrame({"代码": ["000001"]}))
            report_path = Path(folder) / "result.json"
            self.assertEqual(finish(checker, report_path), 1)
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"], dict(passed=1, failed=1, skipped=1))
        self.assertEqual([r["status"] for r in report["checks"]], ["FAIL", "SKIP", "PASS"])
        self.assertEqual(report["checks"][0]["error_type"], "NetworkError")


if __name__ == "__main__":
    unittest.main()

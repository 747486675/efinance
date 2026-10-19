"""Offline contract tests. Every real socket connection is prohibited."""
import ast
import importlib.util
import contextlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import requests

import efinance as ef
from efinance.batch import fetch, legacy_fetch
from efinance.cli import main
from efinance.exceptions import (DataQualityError, NetworkError, ParameterError,
                                 RateLimitError, ResponseError, TimeoutError)
from efinance.export import export_table
from efinance.query import normalize_query, query_one, table_payload
from efinance.transport import FinanceSession, attempts_override
from efinance.validation import validate_table


def blocked(*args, **kwargs):
    raise AssertionError("Offline tests must not connect to the network")


def setUpModule():
    global _socket_patch
    _socket_patch = patch.object(socket.socket, "connect", blocked)
    _socket_patch.start()


def tearDownModule():
    _socket_patch.stop()


def response(status=200, data=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(data).encode("utf-8")
    r._content_consumed = True
    r.headers.update(headers or {})
    r.url = "https://fixture.invalid"
    return r


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.config = ef.get_config()
        ef.configure(requests_per_second=10000)

    def tearDown(self):
        ef.configure(**self.config)

    def test_config_snapshot_and_atomic_validation(self):
        snapshot = ef.get_config()
        snapshot["max_attempts"] = 99
        self.assertEqual(ef.get_config()["max_attempts"], 3)
        for kwargs in (dict(max_attempts=0), dict(max_concurrency=True),
                       dict(read_timeout=float("inf")), dict(validation="bad"), dict(unknown=1)):
            before = ef.get_config()
            with self.assertRaises(ParameterError):
                ef.configure(**kwargs)
            self.assertEqual(before, ef.get_config())
        self.assertIsInstance(ParameterError("bad"), ValueError)

    def test_transport_retries_and_timeout_override(self):
        with patch.object(requests.Session, "request", side_effect=[requests.Timeout("slow"), response(data={"ok": True})]) as send, patch("efinance.transport.time.sleep"):
            value = FinanceSession().get("https://fixture.invalid", timeout=7)
        self.assertTrue(value.json()["ok"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args[1]["timeout"], 7)

    def test_request_config_is_snapshot(self):
        configs = []
        @contextlib.contextmanager
        def slot(config):
            configs.append(dict(config))
            yield
        def send(*args, **kwargs):
            ef.configure(read_timeout=99, max_attempts=1)
            if len(configs) == 1:
                raise requests.Timeout("slow")
            return response(data={})
        with patch("efinance.transport._slot", slot), patch.object(requests.Session, "send", side_effect=send) as request, patch("efinance.transport.time.sleep"):
            FinanceSession().get("https://fixture.invalid")
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0], configs[1])
        self.assertEqual(request.call_args[1]["timeout"], (10, 30))

    def test_retry_after_and_error_cause(self):
        with patch.object(requests.Session, "request", side_effect=[response(429, headers={"Retry-After": "4"}), response(data={})]), patch("efinance.transport.time.sleep") as sleep:
            FinanceSession().get("https://fixture.invalid")
        self.assertGreaterEqual(sleep.call_args[0][0], 4)
        with patch.object(requests.Session, "request", return_value=response(429)), patch("efinance.transport.time.sleep"):
            with self.assertRaises(RateLimitError) as caught:
                FinanceSession().get("https://fixture.invalid", tries=2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertIsInstance(caught.exception.__cause__, requests.HTTPError)

    def test_timeout_exhaustion_and_legacy_override(self):
        with patch.object(requests.Session, "request", side_effect=requests.Timeout("slow")) as send, patch("efinance.transport.time.sleep"):
            with self.assertRaises(TimeoutError) as caught, attempts_override(2):
                FinanceSession().get("https://fixture.invalid")
        self.assertEqual(send.call_count, 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertIsInstance(caught.exception.__cause__, requests.Timeout)

    def test_http_and_json_errors_not_retried(self):
        with patch.object(requests.Session, "request", return_value=response(400)) as send:
            with self.assertRaises(ResponseError):
                FinanceSession().get("https://fixture.invalid")
        self.assertEqual(send.call_count, 1)
        invalid = response()
        invalid._content = b"<html>upstream error</html>"
        with patch.object(requests.Session, "request", return_value=invalid) as send:
            with self.assertRaises(ResponseError):
                FinanceSession().get("https://fixture.invalid").json()
        self.assertEqual(send.call_count, 1)

    def test_retries_pass_gate(self):
        entered = []
        @contextlib.contextmanager
        def slot(config):
            entered.append(1)
            yield
        with patch("efinance.transport._slot", slot), patch.object(requests.Session, "send", side_effect=[response(503), response(data={})]), patch("efinance.transport.time.sleep"):
            FinanceSession().get("https://fixture.invalid")
        self.assertEqual(len(entered), 2)

    def test_process_gate_limits_concurrency_and_spacing(self):
        ef.configure(max_concurrency=2, requests_per_second=50)
        lock = threading.Lock()
        starts, active, peak = [], [0], [0]
        def send(*args, **kwargs):
            with lock:
                starts.append(time.monotonic())
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(.035)
            with lock:
                active[0] -= 1
            return response(data={})
        with patch.object(requests.Session, "send", side_effect=send):
            result = fetch(lambda i: FinanceSession().get("https://fixture.invalid"), range(6), max_workers=2)
        self.assertEqual(len(result.successes), 6)
        self.assertLessEqual(peak[0], 2)
        self.assertTrue(all(b - a >= .017 for a, b in zip(starts, starts[1:])))

    def test_batch_order_dedup_partial_retry(self):
        calls, fail = [], [True]
        def query(code):
            calls.append(code)
            if code == "b" and fail[0]:
                raise NetworkError("failed", attempts=3)
            return code.upper()
        result = fetch(query, ["a", "b", "a", "c"])
        self.assertEqual(list(result.successes), ["a", "c"])
        self.assertEqual(result.failures["b"]["attempts"], 3)
        fail[0] = False
        result.retry_failed()
        self.assertEqual(list(result.successes), ["a", "b", "c"])
        self.assertEqual(calls.count("a"), 1)
        self.assertEqual(calls.count("b"), 2)

    def test_batch_cancel_and_progress(self):
        cancel = threading.Event()
        progress = []
        def query(code):
            cancel.set()
            return code
        result = fetch(query, [1, 2, 3], max_workers=1, cancel_event=cancel,
                       progress=lambda *args: progress.append(args))
        self.assertEqual(list(result.successes), [1])
        self.assertEqual(result.cancelled, [2, 3])
        self.assertEqual(progress, [(1, 3, 1)])

    def test_legacy_batch_exposes_failures(self):
        def query(code):
            if code == "bad":
                raise NetworkError("failed")
            return code
        with self.assertRaises(ef.exceptions.EfinanceError) as caught:
            legacy_fetch(query, ["ok", "bad"])
        self.assertEqual(caught.exception.result.successes["ok"], "ok")

    def test_quality_warn_strict_off_and_required(self):
        frame = pd.DataFrame({"日期": ["2026-01-01", "2026-01-01"], "开盘": [2, 2], "收盘": [2, 2], "最高": [1, 1], "最低": [0, 0]})
        before = frame.copy(deep=True)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertIs(validate_table(frame), frame)
        self.assertTrue(caught)
        pd.testing.assert_frame_equal(frame, before)
        ef.configure(validation="strict")
        with self.assertRaises(DataQualityError):
            validate_table(frame)
        ef.configure(validation="off")
        validate_table(frame)
        with self.assertRaises(ResponseError):
            validate_table(frame, required=["不存在"])

    def test_quote_sample_empty_and_malformed(self):
        from efinance.common.getter import get_quote_history_single
        sample = dict(data=dict(name="样本", klines=["2026-01-02,1,2,3,1,100,200,2,1,1,0.1"]))
        with patch("efinance.common.getter.session.get", return_value=response(data=sample)):
            frame = get_quote_history_single("1.000001", quote_id_mode=True)
        self.assertEqual(frame["代码"].iloc[0], "000001")
        with patch("efinance.common.getter.session.get", return_value=response(data={"data": None})):
            empty = get_quote_history_single("1.000001", quote_id_mode=True)
        self.assertTrue(empty.empty)
        self.assertIn("收盘", empty)
        with patch("efinance.common.getter.session.get", return_value=response(data={})):
            with self.assertRaises(ResponseError):
                get_quote_history_single("1.000001", quote_id_mode=True)

    def test_fund_sample_and_numeric_series(self):
        from efinance.fund import get_quote_history
        sample = dict(Datas=[dict(FSRQ="2026-01-02", DWJZ="1.2", LJJZ="2.4", JZZZL="1")])
        with patch("efinance.fund.getter.fund_session.get", return_value=response(data=sample)):
            frame = get_quote_history("000001")
        self.assertEqual(frame["单位净值"].iloc[0], 1.2)
        @ef.utils.to_numeric
        def data():
            return pd.Series({"基金代码": "000001", "单位净值": "1.2"})
        self.assertEqual(data()["基金代码"], "000001")
        self.assertEqual(data()["单位净值"], 1.2)

    def test_query_validation_precedes_network(self):
        for kwargs in (dict(asset="fund", codes=["000001"], klt=101),
                       dict(asset="stock", codes=["000001"], beg="20260230"),
                       dict(asset="stock", codes=["000001"], beg="20260201", end="20260101"),
                       dict(asset="stock", codes=["000001"], fqt=4),
                       dict(asset="fund", codes=[1])):
            with self.assertRaises(ParameterError):
                normalize_query(**kwargs)

    def test_fund_date_filter(self):
        frame = pd.DataFrame({"日期": ["2026-01-01", "2026-02-01"], "单位净值": [1, 2]})
        with patch("efinance.fund.get_quote_history", return_value=frame):
            result = query_one("000001", "fund", "history", "20260101", "20260131", 101, 1)
        self.assertEqual(len(result), 1)

    def test_json_nonfinite_and_code(self):
        query = normalize_query("stock", ["000001"])
        payload = table_payload(pd.DataFrame({"股票代码": ["000001"], "值": [float("inf")]}), query)
        self.assertEqual(payload["records"][0], {"股票代码": "000001", "值": None})

    def test_asset_routing(self):
        frame = pd.DataFrame({"代码": ["000001"]})
        for asset, target in [("stock", "efinance.stock.get_quote_history"),
                              ("bond", "efinance.bond.get_quote_history"),
                              ("futures", "efinance.futures.get_quote_history")]:
            with patch(target, return_value=frame) as query:
                self.assertIs(query_one("1.000001", asset, "history", "20260101", "20261016", 101, 1), frame)
                query.assert_called_once_with("1.000001", beg="20260101", end="20261016", klt=101, fqt=1)
        with patch("efinance.fund.get_realtime_increase_rate", return_value=frame) as query:
            self.assertIs(query_one("000001", "fund", "quote"), frame)
            query.assert_called_once_with("000001")
        for asset in ("bond", "futures"):
            with patch("efinance.utils.get_quote_id", return_value="1.000001"), patch("efinance.common.getter.get_latest_quote", return_value=frame) as query:
                self.assertIs(query_one("000001", asset, "quote"), frame)
                query.assert_called_once_with("1.000001")

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "optional Excel dependency not installed")
    def test_excel_codes_are_text(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.xlsx"
            export_table(pd.DataFrame({"股票代码": ["000001"], "收盘": [1.2]}), path)
            workbook = load_workbook(path)
            cell = workbook.active["A2"]
            self.assertEqual(cell.value, "000001")
            self.assertEqual(cell.data_type, "s")
            workbook.close()

    @unittest.skipUnless(importlib.util.find_spec("pyarrow"), "optional Parquet dependency not installed")
    def test_parquet_codes_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.parquet"
            export_table(pd.DataFrame({"股票代码": ["000001"]}), path)
            self.assertEqual(pd.read_parquet(path)["股票代码"].iloc[0], "000001")

    def test_optional_export_dependency_message(self):
        with patch.object(pd.DataFrame, "to_excel", side_effect=ImportError("missing")):
            with self.assertRaisesRegex(ParameterError, "efinance\\[excel\\]"):
                export_table(pd.DataFrame({"代码": ["000001"]}), "unused.xlsx")

    def test_old_public_batch_uses_bounded_result(self):
        from efinance.common.getter import get_quote_history_multi
        def history(code, **kwargs):
            if code == "bad":
                raise ResponseError("invalid format")
            return pd.DataFrame({"代码": [code]})
        with patch("efinance.common.getter.get_quote_history_single", side_effect=history):
            with self.assertRaises(ef.exceptions.EfinanceError) as caught:
                get_quote_history_multi(["000001", "bad"])
        self.assertEqual(list(caught.exception.result.successes), ["000001"])

    def test_nonfinite_retry_after_does_not_sleep_forever(self):
        from efinance.transport import _retry_after
        self.assertEqual(_retry_after("Infinity"), 0)
        self.assertEqual(_retry_after("NaN"), 0)

    def test_empty_realtime_has_schema(self):
        from efinance.common.getter import get_realtime_quotes_by_fs
        with patch("efinance.common.getter.session.get", return_value=response(data={"data": {"total": 0, "diff": []}})) as query:
            frame = get_realtime_quotes_by_fs("stock")
        self.assertTrue(frame.empty)
        self.assertIn("行情ID", frame)
        self.assertEqual(query.call_count, 1)

    def test_latest_and_basic_info_require_structural_fields(self):
        from efinance.common.getter import get_latest_quote, get_base_info
        from efinance.fund import get_realtime_increase_rate
        for function, code in ((get_latest_quote, "1.000001"), (get_base_info, "1.000001"),
                               (get_realtime_increase_rate, "000001")):
            with patch("efinance.shared.session.get", return_value=response(data={})):
                with self.assertRaises(ResponseError):
                    function(code)

    def test_csv_bom_and_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.csv"
            frame = pd.DataFrame({"股票代码": ["000001"]})
            export_table(frame, path)
            original = path.read_bytes()
            self.assertTrue(original.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"000001", original)
            with self.assertRaises(FileExistsError):
                export_table(frame, path)
            self.assertEqual(path.read_bytes(), original)
            export_table(frame, path, overwrite=True)

    def test_doctor_offline_and_cli_failures(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["doctor"]), 0)
        self.assertEqual(json.loads(output.getvalue())["online"], "not requested")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["history", "000001", "--asset", "fund", "--fqt", "1"]), 2)
        with patch("efinance.cli.query_one", side_effect=NetworkError("failed")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["quote", "000001"]), 1)

    def test_cli_partial_exports_and_collision(self):
        def query(code, **kwargs):
            if code == "bad":
                raise NetworkError("failed")
            return pd.DataFrame({"股票代码": [code]})
        with tempfile.TemporaryDirectory() as directory:
            with patch("efinance.cli.query_one", side_effect=query), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["quote", "000001", "bad", "--format", "csv", "--output", directory]), 1)
                self.assertEqual(main(["quote", "000001", "bad", "--format", "csv", "--output", directory]), 2)
            self.assertTrue((Path(directory) / "000001.csv").exists())
            self.assertEqual(json.loads((Path(directory) / "failures.json").read_text(encoding="utf-8"))[0]["item"], "bad")

    def test_cli_multicode_stdout_is_one_csv(self):
        def query(code, **kwargs):
            return pd.DataFrame({"股票代码": [code]})
        with patch("efinance.cli.query_one", side_effect=query), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["quote", "000001", "000002", "--format", "csv"]), 0)
        self.assertEqual(output.getvalue().count("\ufeff"), 1)
        self.assertIn("查询标的,股票代码", output.getvalue())
        self.assertIn("000001,000001", output.getvalue())

    def test_no_direct_requests_or_nested_retries(self):
        root = Path(ef.__file__).parent
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    self.assertFalse(node.func.value.id == "requests" and node.func.attr in ("get", "post", "request"), str(path))
                if isinstance(node, ast.Name):
                    self.assertNotEqual(node.id, "retry", str(path))


if __name__ == "__main__":
    unittest.main()

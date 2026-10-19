"""Regression paths from the review; no external network is permitted."""
import contextlib
import importlib.util
import io
import json
import ntpath
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from unittest.mock import patch

import pandas as pd
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ReadTimeoutError

import efinance as ef
from efinance.cli import main
from efinance.exceptions import (DataQualityError, NetworkError, ParameterError,
                                 ResponseError, SymbolNotFoundError, TimeoutError)
from efinance.query import normalize_query, query_one
from efinance.transport import (FinanceSession, _is_timeout, attempts_override,
                                current_attempts, inherit_attempts)
from efinance.validation import response_guard, validate_table
from efinance.common import getter as common

_real_connect = socket.socket.connect
HAS_WEB = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx"))


def offline_connect(sock, address):
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return _real_connect(sock, address)
    raise AssertionError("External network prohibited")


def setUpModule():
    global _network
    _network = patch.object(socket.socket, "connect", offline_connect)
    _network.start()


def tearDownModule():
    _network.stop()


class LocalHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


@contextlib.contextmanager
def delayed_body():
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            release.wait(3)
            try:
                self.wfile.write(b"{}")
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = LocalHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_port)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def history_response():
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"data": {"name": "fixture", "klines": [
        "2026-10-16,1,2,3,1,100,200,2,1,1,0.1"
    ]}}).encode("utf-8")
    response._content_consumed = True
    return response


@contextlib.contextmanager
def truncated_body(chunked=False, recover=False):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(1)
            broken = not recover or len(calls) == 1
            self.send_response(200)
            if broken and chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", "50" if broken else "2")
            self.end_headers()
            self.wfile.write(b"5\r\n{}" if broken and chunked else b"{}")
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = LocalHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_port), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.config = ef.get_config()
        ef.configure(max_attempts=1, connect_timeout=1, read_timeout=.2, requests_per_second=10000)

    def tearDown(self):
        ef.configure(**self.config)

    def test_fund_pdf_downloads_newest_report_with_bounded_listing(self):
        listing = history_response()
        listing._content = json.dumps({"Data": [
            {"ID": "newest", "TITLE": "2026 report"},
            {"ID": "oldest", "TITLE": "2012 report"}]}).encode()
        pdf = history_response()
        pdf._content = b"%PDF-1.4\nfixture"
        with tempfile.TemporaryDirectory() as folder, patch("efinance.fund.getter.fund_session.get", side_effect=[listing, pdf]) as request, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(ef.fund.get_pdf_reports("000001", max_count=1, save_dir=folder))
            self.assertEqual((Path(folder) / "000001" / "2026 report.pdf").read_bytes(), pdf.content)
            self.assertFalse((Path(folder) / "000001" / "2012 report.pdf").exists())
            self.assertEqual(dict(request.call_args_list[0][1]["params"])["pageSize"], "1")
            self.assertIn("H2_newest_1.pdf", request.call_args_list[1][0][0])

    def test_fund_pdf_zero_and_invalid_counts_do_not_request(self):
        with patch("efinance.fund.getter.fund_session.get") as request:
            self.assertIsNone(ef.fund.get_pdf_reports("000001", max_count=0))
            for count in (-1, True, 1.5, None):
                with self.subTest(count=count), self.assertRaises(ParameterError):
                    ef.fund.get_pdf_reports("000001", max_count=count)
        request.assert_not_called()

    def test_fund_pdf_error_pages_do_not_overwrite_existing_pdf(self):
        listing = history_response()
        listing._content = b'{"Data":[{"ID":"newest","TITLE":"2026 report"}]}'
        for content in (b"", b"<html>Access denied</html>", b'{"error":"not found"}'):
            response = history_response()
            response._content = content
            with self.subTest(content=content), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "000001" / "2026 report.pdf"
                target.parent.mkdir()
                target.write_bytes(b"%PDF-1.4\nkeep original")
                with patch("efinance.fund.getter.fund_session.get", side_effect=[listing, response]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(ef.exceptions.EfinanceError) as caught:
                        ef.fund.get_pdf_reports("000001", max_count=1, save_dir=folder)
                self.assertEqual(caught.exception.result.failures[0]["error_type"], "ResponseError")
                self.assertEqual(target.read_bytes(), b"%PDF-1.4\nkeep original")

    def test_fund_pdf_malformed_listing_is_a_response_error(self):
        for data in (None, {}, {"Data": None}, {"Data": {}}, {"Data": [1]}, {"Data": [{}]}):
            response = history_response()
            response._content = json.dumps(data).encode()
            with self.subTest(data=data), patch("efinance.fund.getter.fund_session.get", return_value=response):
                with self.assertRaises(ResponseError):
                    ef.fund.get_pdf_reports("000001", max_count=1)

    def test_market_json_record_types_are_checked_without_quality_validation(self):
        ef.configure(validation="off")
        functions = [
            (lambda: common.get_quote_history_single("0.000001", quote_id_mode=True), "klines", 1),
            (lambda: common.get_history_bill("0.000001"), "klines", 1),
            (lambda: common.get_today_bill("0.000001"), "klines", 1),
            (lambda: common.get_latest_ndays_quote("0.000001", quote_id_mode=True), "trends", 1),
            (lambda: common.get_latest_quote("0.000001"), "diff", "bad")]
        for call, field, bad_record in functions:
            invalid = [None, {}, {"data": 1}, {"data": {}},
                       {"data": {field: {}}}, {"data": {field: "error"}},
                       {"data": {field: None}}, {"data": {field: [bad_record]}},
                       {"data": {"unexpected": {field: []}}}]
            for payload in invalid:
                with self.subTest(field=field, payload=payload):
                    response = history_response()
                    response._content = json.dumps(payload).encode()
                    with patch("efinance.shared.session.get", return_value=response) as request:
                        with self.assertRaises(ResponseError):
                            call()
                    self.assertEqual(request.call_count, 1)

    def test_market_explicit_empty_results_keep_columns(self):
        functions = [
            (lambda: common.get_quote_history_single("0.000001", quote_id_mode=True), "klines"),
            (lambda: common.get_history_bill("0.000001"), "klines"),
            (lambda: common.get_today_bill("0.000001"), "klines"),
            (lambda: common.get_latest_ndays_quote("0.000001", quote_id_mode=True), "trends"),
            (lambda: common.get_latest_quote("0.000001"), "diff")]
        for call, field in functions:
            for payload in ({"data": None}, {"data": {field: []}}):
                response = history_response()
                response._content = json.dumps(payload).encode()
                with self.subTest(field=field, payload=payload), patch("efinance.shared.session.get", return_value=response):
                    frame = call()
                self.assertTrue(frame.empty)
                self.assertIn("代码", frame)
                self.assertIn("名称", frame)

    def test_intraday_and_money_flow_valid_records_preserve_codes(self):
        functions = [
            (lambda: common.get_history_bill("0.000001"), "klines", len(common.EASTMONEY_HISTORY_BILL_FIELDS), "2026-10-16"),
            (lambda: common.get_today_bill("0.000001"), "klines", 6, "09:30:00"),
            (lambda: common.get_latest_ndays_quote("0.000001", quote_id_mode=True), "trends", len(common.EASTMONEY_KLINE_NDAYS_FIELDS), "2026-10-16 09:30")]
        for call, field, count, date in functions:
            payload = {"data": {"name": "fixture", field: [",".join([date] + ["1"] * (count - 1))]}}
            response = history_response()
            response._content = json.dumps(payload).encode()
            with self.subTest(field=field, count=count), patch("efinance.shared.session.get", return_value=response):
                frame = call()
            self.assertEqual(len(frame), 1)
            self.assertEqual(frame["代码"].iloc[0], "000001")
            self.assertEqual(frame["名称"].iloc[0], "fixture")

    def test_truncated_body_retries_both_framings_and_preserves_cause(self):
        from efinance.transport import _slot
        for chunked in (False, True):
            with self.subTest(chunked=chunked), truncated_body(chunked) as (url, calls), FinanceSession() as session:
                session.trust_env = False
                with patch("efinance.transport.time.sleep"), patch("efinance.transport._slot", wraps=_slot) as gate:
                    with self.assertRaises(NetworkError) as caught:
                        session.get(url, tries=2)
                self.assertEqual(len(calls), 2)
                self.assertEqual(gate.call_count, 2)
                self.assertEqual(caught.exception.attempts, 2)
                self.assertIsInstance(caught.exception.__cause__, requests.exceptions.ChunkedEncodingError)
                self.assertNotIsInstance(caught.exception, TimeoutError)

    def test_truncated_body_can_recover_on_next_attempt(self):
        with truncated_body(recover=True) as (url, calls), FinanceSession() as session:
            session.trust_env = False
            observed = []

            def read_in_user_hook(response, **kwargs):
                observed.append(response.content)

            with patch("efinance.transport.time.sleep"):
                response = session.get(url, tries=2, hooks={"response": read_in_user_hook})
            self.assertEqual(response.json(), {})
            self.assertEqual(len(calls), 2)
            self.assertEqual(observed, [b"{}"])

    @unittest.skipUnless(HAS_WEB, "optional web dependencies not installed")
    def test_truncated_body_maps_to_structured_http_502(self):
        from fastapi.testclient import TestClient
        from efinance.web import create_app
        with truncated_body() as (url, calls), FinanceSession() as session:
            session.trust_env = False

            def query(*args, **kwargs):
                return session.get(url, tries=2)

            with patch.dict(os.environ, {"EFINANCE_WEB_USERNAME": "", "EFINANCE_WEB_PASSWORD": ""}), TestClient(create_app()) as client, patch("efinance.web.query_one", side_effect=query), patch("efinance.transport.time.sleep"):
                response = client.post("/api/history", json={"codes": ["1.600519"]})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()["error"]["type"], "NetworkError")
            self.assertEqual(response.json()["error"]["attempts"], 2)
            self.assertEqual(len(calls), 2)

    def test_fund_json_requires_record_list_even_with_validation_off(self):
        ef.configure(validation="off")
        invalid = [None, [], "bad", {}, {"Datas": None}, {"Datas": {}},
                   {"Datas": "bad"}, {"Datas": [1]}, {"Datas": [[]]},
                   {"Datas": [None]}, {"Datas": [{}]}]
        for function in (ef.fund.get_quote_history, ef.fund.get_realtime_increase_rate):
            for data in invalid:
                with self.subTest(function=function.__name__, data=data):
                    response = history_response()
                    response._content = json.dumps(data).encode()
                    with patch("efinance.shared.session.get", return_value=response) as request:
                        with self.assertRaises(ResponseError):
                            function("000001")
                    self.assertEqual(request.call_count, 1)

    def test_fund_empty_record_lists_keep_distinct_schemas(self):
        response = history_response()
        response._content = b'{"Datas":[]}'
        for function, columns in ((ef.fund.get_quote_history, ["日期", "单位净值", "累计净值", "涨跌幅"]),
                                  (ef.fund.get_realtime_increase_rate, ["基金代码", "基金名称", "最新净值", "最新净值公开日期", "估算时间", "估算涨跌幅"])):
            with patch("efinance.shared.session.get", return_value=response):
                frame = function("000001")
            self.assertTrue(frame.empty)
            self.assertEqual(list(frame.columns), columns)

    def test_fund_estimate_valid_records_preserve_leading_zeroes(self):
        response = history_response()
        response._content = json.dumps({"Datas": [{"FCODE": "000001", "SHORTNAME": "fixture", "ACCNAV": "1.2", "PDATE": "2026-10-16", "GZTIME": "2026-10-16 15:00", "GSZZL": "0.5"}]}).encode()
        with patch("efinance.shared.session.get", return_value=response):
            frame = ef.fund.get_realtime_increase_rate("000001")
        self.assertEqual(frame["基金代码"].iloc[0], "000001")
        self.assertEqual(frame["估算涨跌幅"].iloc[0], .5)

    def test_cli_export_failure_keeps_other_outputs_and_query_failures(self):
        def query(code, **kwargs):
            if code == "000003":
                raise NetworkError("fixture query failure", attempts=2)
            return pd.DataFrame({"股票代码": [code]})

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "000001.json").mkdir()
            with patch("efinance.cli.query_one", side_effect=query), contextlib.redirect_stderr(io.StringIO()) as errors, contextlib.redirect_stdout(io.StringIO()) as output:
                result = main(["quote", "000001", "000002", "000003", "--output", folder, "--overwrite"])
            self.assertEqual(result, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(json.loads((root / "000002.json").read_text(encoding="utf-8"))[0]["股票代码"], "000002")
            failures = json.loads((root / "failures.json").read_text(encoding="utf-8"))
            self.assertEqual([f["item"] for f in failures], ["000001", "000003"])
            self.assertEqual(failures[0]["stage"], "export")
            self.assertEqual(failures[0]["attempts"], 0)
            self.assertEqual(failures[0]["path"], str(root / "000001.json"))
            self.assertEqual(failures[1]["attempts"], 2)
            self.assertEqual(json.loads(errors.getvalue()), failures)

    def test_cli_export_serialization_error_is_recorded(self):
        from efinance.export import export_table

        def export(frame, path, *args):
            if Path(path).stem == "000001":
                raise ValueError("fixture serialization failure")
            return export_table(frame, path, *args)

        with tempfile.TemporaryDirectory() as folder, patch("efinance.cli.query_one", return_value=pd.DataFrame({"代码": ["000002"]})), patch("efinance.cli.export_table", side_effect=export), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["quote", "000001", "000002", "--output", folder]), 1)
            self.assertTrue((Path(folder) / "000002.json").is_file())
            failures = json.loads((Path(folder) / "failures.json").read_text(encoding="utf-8"))
            self.assertEqual(failures[0]["error_type"], "ValueError")
            self.assertEqual(failures[0]["stage"], "export")

    def test_cli_unwritable_manifest_keeps_failures_on_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "000001.json").mkdir()
            (root / "failures.json").mkdir()
            with patch("efinance.cli.query_one", return_value=pd.DataFrame({"代码": ["000002"]})), contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["quote", "000001", "000002", "--output", folder, "--overwrite"]), 1)
            self.assertTrue((root / "000002.json").is_file())
            failures = json.loads(errors.getvalue().splitlines()[0])
            self.assertEqual(failures[0]["item"], "000001")
            self.assertEqual(failures[0]["stage"], "export")
            self.assertIn("failures.json", errors.getvalue())

    def test_cli_partial_parameter_failure_returns_partial_failure_code(self):
        def query(code, **kwargs):
            if code == "bad":
                raise ParameterError("bad symbol")
            return pd.DataFrame({"代码": [code]})

        with patch("efinance.cli.query_one", side_effect=query), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["quote", "000001", "bad"]), 1)
            self.assertEqual(main(["quote", "bad"]), 2)

    def test_windows_case_collision_is_rejected_before_query(self):
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite), tempfile.TemporaryDirectory() as folder:
                existing = Path(folder) / "AAPL.json"
                existing.write_text("keep existing data", encoding="utf-8")
                args = ["quote", "AAPL", "aapl", "--output", folder, "--format", "json"]
                if overwrite:
                    args.append("--overwrite")
                with patch("efinance.cli.os.path.normcase", ntpath.normcase), patch("efinance.cli.query_one") as query, contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(main(args), 2)
                query.assert_not_called()
                self.assertIn("duplicate export filenames", errors.getvalue())
                self.assertEqual(existing.read_text(encoding="utf-8"), "keep existing data")
                self.assertFalse((Path(folder) / "failures.json").exists())

    def test_manifest_filename_cannot_collide_with_a_symbol(self):
        with tempfile.TemporaryDirectory() as folder, patch("efinance.cli.query_one") as query, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["quote", "failures", "AAPL", "--output", folder, "--format", "json", "--overwrite"]), 2)
            query.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_csv_uses_utf8_bytes_despite_gbk_text_stdout(self):
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding="gbk")
        frame = pd.DataFrame({"股票代码": ["000001"], "股票名称": ["平安银行"]})
        try:
            with patch("efinance.cli.query_one", return_value=frame), contextlib.redirect_stdout(output):
                self.assertEqual(main(["quote", "000001", "--format", "csv"]), 0)
            data = raw.getvalue()
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(data.decode("utf-8-sig"), frame.to_csv(index=False))
        finally:
            output.detach()

    def test_csv_in_redirected_subprocess_is_utf8(self):
        script = """
from unittest.mock import patch
import pandas as pd
from efinance.cli import main
frame = pd.DataFrame({"股票代码": ["000001"], "股票名称": ["平安银行"]})
with patch("efinance.cli.query_one", return_value=frame):
    raise SystemExit(main(["quote", "000001", "--format", "csv"]))
"""
        environment = dict(os.environ, PYTHONIOENCODING="gbk")
        result = subprocess.run([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=environment, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("gbk", errors="replace"))
        self.assertEqual(result.stdout.count(b"\xef\xbb\xbf"), 1)
        self.assertIn("000001,平安银行", result.stdout.decode("utf-8-sig"))

    def test_cancelled_retry_preserves_failure_then_success_clears_it(self):
        failing, stop, calls = [True], threading.Event(), Counter()

        def work(code):
            calls[code] += 1
            if code == "failed" and failing[0]:
                raise NetworkError("fixture", attempts=2)
            return code

        def progress(completed, total, item):
            if item == "already":
                stop.set()

        result = ef.batch.fetch(work, ["failed", "already", "unstarted"], max_workers=1,
                                cancel_event=stop, progress=progress)
        self.assertEqual(result.cancelled, ["unstarted"])
        result.retry_failed()  # Event is still set; no new work may run.
        self.assertEqual(list(result.failures), ["failed"])
        self.assertEqual(result.cancelled, ["unstarted"])
        self.assertEqual(calls["failed"], 1)
        failing[0] = False
        stop.clear()
        result.retry_failed()
        self.assertEqual(list(result.successes), ["failed", "already"])
        self.assertEqual(result.failures, {})
        self.assertEqual(result.cancelled, ["unstarted"])
        self.assertTrue(set(result.successes).isdisjoint(result.cancelled))
        self.assertEqual(calls, Counter(failed=2, already=1))

    def test_futures_history_resolves_code_before_upstream(self):
        query = normalize_query("futures", ["rbm"])
        kwargs = {k: v for k, v in query.items() if k != "codes"}
        with patch("efinance.utils.get_quote_id", return_value="113.rbm") as resolve, patch("efinance.shared.session.get", return_value=history_response()) as request:
            frame = query_one("rbm", **kwargs)
        resolve.assert_called_once_with("rbm")
        self.assertEqual(dict(request.call_args[1]["params"])["secid"], "113.rbm")
        self.assertEqual(frame["期货代码"].iloc[0], "rbm")
        self.assertEqual(len(frame), 1)

    def test_futures_complete_id_still_works_without_index_build(self):
        with patch("efinance.utils.load_quote_index", side_effect=AssertionError("unexpected index build")), patch("efinance.shared.session.get", return_value=history_response()) as request:
            frame = query_one("113.rbm", "futures", "history", "20261001", "20261016", 101, 1)
        self.assertEqual(dict(request.call_args[1]["params"])["secid"], "113.rbm")
        self.assertFalse(frame.empty)

    def test_unknown_futures_code_fails_before_history_request(self):
        with patch("efinance.utils.get_quote_id", return_value=""), patch("efinance.shared.session.get") as request:
            with self.assertRaises(SymbolNotFoundError):
                query_one("unknown", "futures", "history", "20261001", "20261016", 101, 1)
        request.assert_not_called()

    def test_cli_futures_history_resolves_code(self):
        with patch("efinance.utils.get_quote_id", return_value="113.rbm"), patch("efinance.shared.session.get", return_value=history_response()) as request, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["history", "rbm", "--asset", "futures"]), 0)
        self.assertEqual(dict(request.call_args[1]["params"])["secid"], "113.rbm")
        self.assertEqual(json.loads(output.getvalue())["records"][0]["期货代码"], "rbm")

    def test_wrapped_read_timeout_retries_and_preserves_cause(self):
        cause = requests.ConnectionError(ReadTimeoutError(None, "fixture", "read timed out"))
        with patch.object(requests.Session, "request", side_effect=cause) as request, patch("efinance.transport.time.sleep"):
            with self.assertRaises(TimeoutError) as caught:
                FinanceSession().get("https://fixture.invalid", tries=2)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertIs(caught.exception.__cause__, cause)

    def test_real_response_body_timeout_is_classified(self):
        with delayed_body() as url, FinanceSession() as session:
            session.trust_env = False
            with self.assertRaises(TimeoutError) as caught:
                session.get(url)
        self.assertIsInstance(caught.exception.__cause__, requests.ConnectionError)
        self.assertEqual(caught.exception.attempts, 1)

    def test_connection_refusal_is_not_a_timeout(self):
        reason = NewConnectionError(None, "connection refused")
        cause = requests.ConnectionError(MaxRetryError(None, "fixture", reason))
        self.assertFalse(_is_timeout(cause))
        with patch.object(requests.Session, "request", side_effect=cause):
            with self.assertRaises(NetworkError) as caught:
                FinanceSession().get("https://fixture.invalid")
        self.assertNotIsInstance(caught.exception, TimeoutError)

    def test_timeout_chain_can_contain_cycles(self):
        cause = requests.ConnectionError("connection reset")
        cause.__context__ = cause
        self.assertFalse(_is_timeout(cause))
        cause.__cause__ = ReadTimeoutError(None, "fixture", "read timed out")
        self.assertTrue(_is_timeout(cause))

    def test_pagination_inherits_explicit_attempt_budget(self):
        ef.configure(max_attempts=3)
        for budget in (1, 2):
            with self.subTest(budget=budget):
                calls = []

                def request(*args, **kwargs):
                    calls.append(current_attempts())
                    if len(calls) == 1:
                        response = history_response()
                        response._content = json.dumps({"data": {"total": 2, "diff": [{}]}}).encode()
                        return response
                    raise requests.Timeout("page timeout")

                with patch.object(requests.Session, "request", side_effect=request), patch("efinance.transport.time.sleep"):
                    result = ef.batch.fetch(ef.stock.get_realtime_quotes, ["沪A"], tries=budget)
                self.assertEqual(calls, [budget] * (1 + budget))
                self.assertEqual(result.failures["沪A"]["attempts"], budget)
                self.assertEqual(result.failures["沪A"]["error_type"], "TimeoutError")

    def test_report_pagination_also_inherits_budget(self):
        ef.configure(max_attempts=3)
        for function in (ef.stock.get_all_company_performance, ef.stock.get_latest_holder_number):
            with self.subTest(function=function.__name__):
                calls = []
                pages_started = threading.Barrier(2, timeout=5)

                def request(*args, **kwargs):
                    calls.append(current_attempts())
                    if len(calls) == 1:
                        response = history_response()
                        response._content = json.dumps({"result": {"count": 2, "data": [{}]}}).encode()
                        return response
                    # Both pages must enter before either fails. Otherwise
                    # executor.map may legitimately cancel an unstarted page.
                    pages_started.wait()
                    raise requests.Timeout("page timeout")

                dates = pd.DataFrame({"报告日期": ["2026-06-30"]})
                with patch("efinance.stock.getter.get_all_report_dates", return_value=dates), patch.object(requests.Session, "request", side_effect=request), patch("efinance.transport.time.sleep"):
                    result = ef.batch.fetch(function, [None], tries=1)
                self.assertEqual(calls, [1, 1, 1])
                self.assertEqual(result.failures[None]["attempts"], 1)
                self.assertEqual(result.failures[None]["error_type"], "TimeoutError")

    def test_deal_detail_cache_hit_does_not_fetch_base_info(self):
        base = pd.Series({"代码": "600519", "名称": "fixture"})
        response = history_response()
        response._content = b'{"data":{"details":[],"prePrice":1}}'
        with patch.dict(common.BASE_INFO_CACHE, {"1.600519": base}, clear=True), patch.object(common, "get_base_info", side_effect=NetworkError("unavailable")) as fetch, patch("efinance.shared.session.get", return_value=response):
            frame = common.get_deal_detail("1.600519")
        fetch.assert_not_called()
        self.assertTrue(frame.empty)
        self.assertIn("代码", frame)

    def test_deal_detail_cache_miss_fetches_once(self):
        base = pd.Series({"代码": "000001", "名称": "fixture"})
        response = history_response()
        response._content = b'{"data":{"details":["09:30:00,1,2,3"],"prePrice":1}}'
        with patch.dict(common.BASE_INFO_CACHE, {}, clear=True), patch.object(common, "get_base_info", return_value=base) as fetch, patch("efinance.shared.session.get", return_value=response):
            for _ in range(2):
                frame = common.get_deal_detail("0.000001")
                self.assertEqual(frame["代码"].iloc[0], "000001")
            self.assertIs(common.BASE_INFO_CACHE["0.000001"], base)
        fetch.assert_called_once_with("0.000001")

    def test_redirect_hops_share_rate_and_configuration_snapshot(self):
        starts, cookies = [], []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                starts.append(time.monotonic())
                cookies.append(self.headers.get("Cookie"))
                if self.path == "/start":
                    ef.configure(requests_per_second=10000)
                target = {"/start": "/middle", "/middle": "/final"}.get(self.path)
                self.send_response(302 if target else 200)
                if target:
                    self.send_header("Location", target)
                    self.send_header("Set-Cookie", "fixture=yes; Path=/")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            ef.configure(max_concurrency=1, requests_per_second=20)
            with FinanceSession() as session:
                session.trust_env = False
                url = "http://127.0.0.1:{}/start".format(server.server_port)
                response = session.get(url)
                self.assertEqual(response.json(), {})
                self.assertTrue(response.url.endswith("/final"))
                self.assertEqual([r.status_code for r in response.history], [302, 302])
                self.assertEqual(len(starts), 3)
                self.assertTrue(all(b - a >= .04 for a, b in zip(starts, starts[1:])))
                self.assertIn("fixture=yes", cookies[-1])
                self.assertIsNone(response.next)
                response = session.get(url, allow_redirects=False)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(len(starts), 4)
                self.assertTrue(response.next.url.endswith("/middle"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_non_json_endpoints_reject_error_pages_and_bad_structures(self):
        fixtures = [b'<html><title>Access denied</title>rate limited</html>',
                    b'garbage {"fivequote":{},"realtimequote":{}}',
                    b'{"unexpected":[]}', b'null']
        for body in fixtures:
            for function, args in ((ef.fund.get_fund_codes, ()), (ef.stock.get_quote_snapshot, ("600519",))):
                with self.subTest(body=body, function=function.__name__):
                    response = history_response()
                    response._content = body
                    with patch("efinance.shared.session.get", return_value=response):
                        with self.assertRaises(ResponseError):
                            function(*args)

    def test_fund_codes_accepts_valid_js_and_preserves_empty_schema(self):
        for datas in ([], ["000001,fixture,extra", "000002,second,extra"]):
            response = history_response()
            response._content = ('var rankData = {datas:' + json.dumps(datas) + ',allRecords:2};').encode()
            with patch("efinance.shared.session.get", return_value=response):
                frame = ef.fund.get_fund_codes()
            self.assertEqual(list(frame.columns), ["基金代码", "基金简称"])
            self.assertEqual(list(frame["基金代码"]), [row.split(",")[0] for row in datas])

    def test_fund_codes_rejects_missing_or_malformed_datas(self):
        for body in (b'var rankData={allRecords:0};', b'var rankData={datas:null};',
                     b'var rankData={datas:[123]};', b'var rankData={datas:["bad"]};',
                     b'var rankData={datas:[]garbage};'):
            response = history_response()
            response._content = body
            with self.subTest(body=body), patch("efinance.shared.session.get", return_value=response):
                with self.assertRaises(ResponseError):
                    ef.fund.get_fund_codes()

    def test_snapshot_explicit_empty_is_distinct_from_invalid_shape(self):
        for data in ({"fivequote": None, "realtimequote": None},
                     {"fivequote": {}, "realtimequote": {}}):
            response = history_response()
            response._content = ('jQuery183026310160411569883_1646052793441(' + json.dumps(data) + ');').encode()
            with patch("efinance.shared.session.get", return_value=response):
                value = ef.stock.get_quote_snapshot("600519")
            self.assertEqual(len(value), 37)
            self.assertTrue(value.isna().all())
        for data in ({"fivequote": []}, {"fivequote": [1], "realtimequote": {"code": "600519"}}):
            response._content = json.dumps(data).encode()
            with patch("efinance.shared.session.get", return_value=response):
                with self.assertRaises(ResponseError):
                    ef.stock.get_quote_snapshot("600519")

    def test_snapshot_valid_jsonp_preserves_fields_and_code(self):
        fields = ("code name time zd zdf currentPrice yesClosePrice openPrice open high low avg topprice bottomprice turnover volume amount "
                  "sale1 sale2 sale3 sale4 sale5 buy1 buy2 buy3 buy4 buy5 "
                  "sale1_count sale2_count sale3_count sale4_count sale5_count "
                  "buy1_count buy2_count buy3_count buy4_count buy5_count").split()
        data = {key: "1" for key in fields}
        data.update(code="000001", name="fixture", time="09:30:00")
        body = {"fivequote": {"buy1": data.pop("buy1")}, "realtimequote": data}
        response = history_response()
        response._content = ('jQuery183026310160411569883_1646052793441(' + json.dumps(body) + ');').encode()
        with patch("efinance.shared.session.get", return_value=response):
            value = ef.stock.get_quote_snapshot("000001")
        self.assertEqual(value["代码"], "000001")
        self.assertEqual(value["买1价"], 1.)

    def test_ohlc_nonfinite_strict_warn_off_and_negative_prices(self):
        for column in ("开盘", "收盘", "最高", "最低"):
            for bad in (float("inf"), float("-inf"), "Infinity"):
                frame = pd.DataFrame({"开盘": [1.], "收盘": [1.], "最高": [2.], "最低": [0.]})
                frame[column] = bad
                original = frame.copy(deep=True)
                ef.configure(validation="strict")
                with self.subTest(column=column, bad=bad), self.assertRaises(DataQualityError):
                    validate_table(frame)
                ef.configure(validation="warn")
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    self.assertIs(validate_table(frame), frame)
                self.assertIn("nonfinite OHLC", str(caught[0].message))
                ef.configure(validation="off")
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    validate_table(frame)
                self.assertEqual(caught, [])
                pd.testing.assert_frame_equal(frame, original)
        ef.configure(validation="strict")
        frame = pd.DataFrame({"开盘": [-2.], "收盘": [-1.], "最高": [-.5], "最低": [-3.]})
        self.assertIs(validate_table(frame), frame)

    def test_inherited_context_is_restored_in_reused_worker(self):
        with ThreadPoolExecutor(max_workers=1) as executor:
            for budget in (2, 1):
                with attempts_override(budget):
                    work = inherit_attempts(current_attempts)
                self.assertEqual(executor.submit(work).result(), budget)
                self.assertIsNone(executor.submit(current_attempts).result())

    def test_nested_batch_inherits_parent_attempt_budget(self):
        ef.configure(max_attempts=3)

        def outer(item):
            return ef.batch.fetch(lambda code: FinanceSession().get("https://fixture.invalid"), ["child"])

        with patch.object(requests.Session, "request", side_effect=requests.Timeout("fixture")) as request, patch("efinance.transport.time.sleep"):
            result = ef.batch.fetch(outer, ["parent"], tries=1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result.successes["parent"].failures["child"]["attempts"], 1)

    def test_json_uses_utf8_bytes_despite_gbk_stdout(self):
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding="gbk")
        frame = pd.DataFrame({"股票代码": ["000001"], "股票名称": ["平安银行🙂"]})
        try:
            with patch("efinance.cli.query_one", return_value=frame), contextlib.redirect_stdout(output):
                self.assertEqual(main(["quote", "000001"]), 0)
            self.assertFalse(raw.getvalue().startswith(b"\xef\xbb\xbf"))
            payload = json.loads(raw.getvalue().decode("utf-8"))
            self.assertEqual(payload["records"][0]["股票名称"], "平安银行🙂")
        finally:
            output.detach()

    def test_multicode_json_redirect_is_utf8_ndjson(self):
        script = """
from unittest.mock import patch
import socket
import pandas as pd
from efinance.cli import main
def blocked(*args, **kwargs):
    raise AssertionError("external network prohibited")
socket.socket.connect = blocked
def query(code, **kwargs):
    return pd.DataFrame({"股票代码": [code], "股票名称": ["平安银行🙂"]})
with patch("efinance.cli.query_one", side_effect=query):
    raise SystemExit(main(["quote", "000001", "000002"]))
"""
        result = subprocess.run([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=dict(os.environ, PYTHONIOENCODING="gbk"), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("gbk", errors="replace"))
        records = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
        self.assertEqual([r["code"] for r in records], ["000001", "000002"])
        self.assertEqual(records[0]["records"][0]["股票名称"], "平安银行🙂")

    def test_strict_fund_history_rejects_missing_or_invalid_unit_nav(self):
        ef.configure(validation="strict")
        for unit in (None, "", "--", "bad", "NaN", "inf", "Infinity"):
            with self.subTest(unit=unit):
                response = history_response()
                response._content = json.dumps({"Datas": [{"FSRQ": "2026-10-16", "DWJZ": unit, "LJJZ": None, "JZZZL": "--"}]}).encode()
                with patch("efinance.shared.session.get", return_value=response):
                    with self.assertRaises(DataQualityError):
                        ef.fund.get_quote_history("000001")
        # Non-finite floats are invalid JSON literals for strict decoders;
        # exercise those already-decoded numeric values at the table boundary.
        for unit in (float("nan"), float("inf")):
            with self.subTest(unit=unit), self.assertRaises(DataQualityError):
                validate_table(pd.DataFrame({"日期": ["2026-10-16"], "单位净值": [unit]}))

    def test_nav_warn_and_off_do_not_modify_data(self):
        frame = pd.DataFrame({"日期": ["2026-10-16"], "单位净值": [None], "累计净值": [None], "涨跌幅": ["--"]})
        original = frame.copy(deep=True)
        ef.configure(validation="warn")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertIs(validate_table(frame), frame)
        self.assertEqual(len(caught), 1)
        self.assertIn("unit NAV", str(caught[0].message))
        pd.testing.assert_frame_equal(frame, original)
        ef.configure(validation="off")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            validate_table(frame)
        self.assertEqual(caught, [])
        pd.testing.assert_frame_equal(frame, original)

    def test_cumulative_nav_and_daily_change_can_be_missing(self):
        ef.configure(validation="strict")
        for cumulative in (None, "", "-", "--", "2.4"):
            with self.subTest(cumulative=cumulative):
                response = history_response()
                response._content = json.dumps({"Datas": [{"FSRQ": "2026-10-16", "DWJZ": "1.2", "LJJZ": cumulative, "JZZZL": "--"}]}).encode()
                with patch("efinance.shared.session.get", return_value=response):
                    frame = ef.fund.get_quote_history("000001")
                self.assertEqual(frame["单位净值"].iloc[0], 1.2)

    def test_supplied_cumulative_nav_must_be_numeric_and_finite(self):
        ef.configure(validation="strict")
        for cumulative in ("bad", float("inf"), "inf"):
            with self.subTest(cumulative=cumulative):
                frame = pd.DataFrame({"日期": ["2026-10-16"], "单位净值": [1.2], "累计净值": [cumulative]})
                with self.assertRaises(DataQualityError):
                    validate_table(frame)

    def test_fund_empty_and_missing_required_field_are_distinct(self):
        ef.configure(validation="off")
        response = history_response()
        response._content = b'{"Datas": []}'
        with patch("efinance.shared.session.get", return_value=response):
            frame = ef.fund.get_quote_history("000001")
        self.assertTrue(frame.empty)
        self.assertIn("单位净值", frame)
        response._content = json.dumps({"Datas": [{"FSRQ": "2026-10-16", "LJJZ": None, "JZZZL": "--"}]}).encode()
        with patch("efinance.shared.session.get", return_value=response):
            with self.assertRaises(ResponseError):
                ef.fund.get_quote_history("000001")

    def test_invalid_sdk_arguments_are_parameters_before_network(self):
        calls = [lambda: ef.stock.get_quote_history(None),
                 lambda: ef.stock.get_quote_history(),
                 lambda: ef.stock.get_base_info(None),
                 lambda: ef.fund.get_base_info(None),
                 lambda: ef.bond.get_base_info(None),
                 lambda: ef.futures.get_quote_history(None),
                 lambda: ef.stock.get_latest_quote(None),
                 lambda: ef.utils.get_quote_id(None),
                 lambda: ef.fund.get_quote_history("000001", unexpected=True),
                 lambda: ef.stock.get_quote_history("1.600519", beg="20261301")]
        with patch("efinance.shared.session.get") as get, patch("efinance.shared.session.post") as post:
            for call in calls:
                with self.subTest(call=call):
                    with self.assertRaises(ParameterError):
                        call()
            get.assert_not_called()
            post.assert_not_called()

    def test_local_type_and_value_errors_are_not_response_errors(self):
        for kind in (TypeError, ValueError):
            @response_guard
            def local_error():
                raise kind("implementation error")

            with self.subTest(kind=kind), self.assertRaises(kind) as caught:
                local_error()
            self.assertNotIsInstance(caught.exception, ef.exceptions.EfinanceError)

    def test_bad_kline_shape_remains_a_response_error(self):
        for data in (1, {"name": "fixture", "klines": ["2026-10-16,1"]}, {"name": "fixture", "klines": [1]}):
            with self.subTest(data=data):
                response = history_response()
                response._content = json.dumps({"data": data}).encode()
                with patch("efinance.shared.session.get", return_value=response):
                    with self.assertRaises(ResponseError) as caught:
                        ef.stock.get_quote_history("1.600519")
                self.assertIsNotNone(caught.exception.__cause__)

    @unittest.skipUnless(HAS_WEB, "optional web dependencies not installed")
    def test_body_read_timeout_maps_to_http_504(self):
        from fastapi.testclient import TestClient
        from efinance.web import create_app
        cause = requests.ConnectionError(ReadTimeoutError(None, "fixture", "read timed out"))

        def query(*args, **kwargs):
            return FinanceSession().get("https://fixture.invalid")

        with patch.dict(os.environ, {"EFINANCE_WEB_USERNAME": "", "EFINANCE_WEB_PASSWORD": ""}), TestClient(create_app()) as client, patch("efinance.web.query_one", side_effect=query), patch.object(requests.Session, "request", side_effect=cause):
            response = client.post("/api/history", json={"codes": ["1.600519"]})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["type"], "TimeoutError")


if __name__ == "__main__":
    unittest.main()

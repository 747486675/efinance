"""Offline index integrity and concurrent name lookup, with isolated caches."""
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from efinance import utils
from efinance.common.config import QUOTE_INDEX_GROUPS, MarketType


DATA = {
    "000001": [
        {"q": "0.000001", "n": "平安银行", "t": "", "c": "AStock"},
        {"q": "1.000001", "n": "上证指数", "t": "指数", "c": "Index"}],
    "600519": [{"q": "1.600519", "n": "贵州茅台", "t": "", "c": "AStock"}]
}


class IndexAcceptanceTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Index acceptance must stay offline"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_name_lookup_waits_until_disk_index_and_names_are_ready(self):
        entered, release, started, finished = (threading.Event() for _ in range(4))
        original = utils._index_names

        def paused_names():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("index publication test timed out")
            original()

        def lookup():
            started.set()
            try:
                return utils.search_quote_from_index("上证指数")
            finally:
                finished.set()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.json"
            path.write_text(json.dumps(dict(version=2, last_time=time.time(), data=DATA)), encoding="utf-8")
            with patch.object(utils, "QUOTE_INDEX_CACHE_PATH", str(path)), patch.dict(utils.QUOTE_INDEX, {}, clear=True), patch.dict(utils.QUOTE_INDEX_BY_NAME, {}, clear=True), patch.object(utils, "_index_names", side_effect=paused_names), patch("efinance.stock.getter.get_realtime_quotes", side_effect=AssertionError("unexpected network")), ThreadPoolExecutor(max_workers=2) as pool:
                loader = pool.submit(utils.load_quote_index)
                try:
                    self.assertTrue(entered.wait(2))
                    reader = pool.submit(lookup)
                    self.assertTrue(started.wait(2))
                    self.assertFalse(finished.wait(.1), "Name lookup observed a half-loaded index")
                finally:
                    release.set()
                loader.result(timeout=2)
                self.assertEqual(reader.result(timeout=2)[0].quote_id, "1.000001")

    def test_seven_group_build_retains_candidates_and_round_trips(self):
        calls = []

        def quotes(fs=None):
            calls.append(fs)
            if fs == QUOTE_INDEX_GROUPS[0][0]:
                return pd.DataFrame({"代码": ["000001", "600519"], "名称": ["平安银行", "贵州茅台"], "行情ID": ["0.000001", "1.600519"]})
            if fs == "沪深系列指数":
                return pd.DataFrame({"代码": ["000001"], "名称": ["上证指数"], "行情ID": ["1.000001"]})
            return pd.DataFrame({"代码": ["fixture"], "名称": [fs], "行情ID": ["1.fixture"]})

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.json"
            with patch.object(utils, "QUOTE_INDEX_CACHE_PATH", str(path)), patch.dict(utils.QUOTE_INDEX, {}, clear=True), patch.dict(utils.QUOTE_INDEX_BY_NAME, {}, clear=True), patch("efinance.stock.getter.get_realtime_quotes", side_effect=quotes):
                rebuilt = utils.load_quote_index(force_reload=True)
                self.assertEqual(set(calls), {group[0] for group in QUOTE_INDEX_GROUPS})
                self.assertEqual(len(calls), 7)
                self.assertEqual([item["q"] for item in rebuilt["000001"]], ["0.000001", "1.000001"])
                self.assertEqual(utils.search_quote_from_index("上证指数")[0].quote_id, "1.000001")
                self.assertEqual(utils.search_quote_from_index("000001", market_type=MarketType.index)[0].quote_id, "1.000001")
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["version"], 2)
                expected = saved["data"]
                utils.QUOTE_INDEX.clear()
                utils.QUOTE_INDEX_BY_NAME.clear()
                with patch("efinance.stock.getter.get_realtime_quotes", side_effect=AssertionError("reload queried upstream")):
                    self.assertEqual(utils.load_quote_index(), expected)
                    names = ["平安银行", "上证指数"] * 4
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        values = list(pool.map(utils.search_quote_from_index, names))
                    self.assertEqual([value[0].quote_id for value in values], ["0.000001", "1.000001"] * 4)


if __name__ == "__main__":
    unittest.main()

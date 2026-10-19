"""Real seven-market rebuild and candidate acceptance, isolated from user caches."""
import argparse
import hashlib
import json
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import efinance as ef
from efinance import utils
from efinance.stock import getter as stock_getter
from efinance.common.config import QUOTE_INDEX_GROUPS, MarketType


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/index-rebuild-acceptance-results.json"))
    args = parser.parse_args()
    config, old_path = ef.get_config(), utils.QUOTE_INDEX_CACHE_PATH
    original_index, original_names = dict(utils.QUOTE_INDEX), dict(utils.QUOTE_INDEX_BY_NAME)
    original_quotes = stock_getter.get_realtime_quotes
    user_path = Path(old_path)
    original_fingerprint = fingerprint(user_path)
    report = dict(checked_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                  isolated_temporary_cache=True, groups=[], checks=[])
    lock = threading.Lock()
    ef.configure(connect_timeout=5, read_timeout=10, max_attempts=2,
                 max_concurrency=2, requests_per_second=1)
    report["config"] = ef.get_config()

    def observed_quotes(*positional, **kwargs):
        record = dict(market=kwargs.get("fs"))
        started = time.monotonic()
        try:
            frame = original_quotes(*positional, **kwargs)
            record.update(status="PASS" if not frame.empty else "EMPTY", rows=len(frame))
            return frame
        except Exception as error:
            record.update(status="FAIL", error_type=type(error).__name__, message=str(error))
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            with lock:
                report["groups"].append(record)
                print(record["status"], record["market"], record.get("rows", ""), flush=True)

    def check(name, function):
        record = dict(name=name)
        try:
            details = function()
            record.update(status="PASS", **(details or {}))
        except Exception as error:
            record.update(status="FAIL", error_type=type(error).__name__, message=str(error))
        report["checks"].append(record)
        print(record["status"], name, flush=True)

    try:
        stock_getter.get_realtime_quotes = observed_quotes
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "quote-index.json"
            utils.QUOTE_INDEX_CACHE_PATH = str(cache_path)
            utils.QUOTE_INDEX.clear()
            utils.QUOTE_INDEX_BY_NAME.clear()
            try:
                index = utils.load_quote_index(force_reload=True)
            except Exception as error:
                report["checks"].append(dict(name="fresh_rebuild", status="FAIL", error_type=type(error).__name__, message=str(error)))
                index = {}
            complete = (len(report["groups"]) == len(QUOTE_INDEX_GROUPS)
                        and all(group["status"] == "PASS" for group in report["groups"]))
            report.update(complete_market_fetch=complete, codes=len(index), cache_written=cache_path.exists(),
                          duplicate_codes=sum(len(entries) > 1 for entries in index.values()))

            def cache_contract():
                assert complete, "One or more markets failed or were empty; partial data is not a complete rebuild"
                assert cache_path.exists(), "Complete rebuild did not write its isolated cache"
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                assert cache["version"] == utils.QUOTE_INDEX_VERSION
                assert cache["data"] == index
                assert time.time() - cache["last_time"] < 600
                assert all(len({e["q"] for e in entries}) == len(entries) for entries in index.values())
                return dict(codes=len(index), version=cache["version"])
            check("complete_rebuild_and_cache_format", cache_contract)

            if index:
                def candidates():
                    quotes = utils.search_quote_from_index("000001")
                    ids = [quote.quote_id for quote in quotes]
                    assert len(ids) >= 2, "000001 did not retain multiple market candidates"
                    assert ids[0] == "0.000001" and "1.000001" in ids
                    for entry in index["000001"]:
                        values = utils.search_quote_from_index(entry["n"])
                        assert values and values[0].quote_id == entry["q"], "Name did not prioritize its matching candidate"
                    assert utils.search_quote_from_index("000001", market_type=MarketType.index)[0].quote_id == "1.000001"
                    return dict(code="000001", quote_ids=ids,
                                names=[quote.name for quote in quotes])
                check("multiple_candidates_names_and_market_filter", candidates)
            else:
                report["checks"].append(dict(name="multiple_candidates_names_and_market_filter", status="SKIP", reason="No rebuilt index"))

            if complete and cache_path.exists():
                expected = dict(index)
                calls_before = len(report["groups"])
                utils.QUOTE_INDEX.clear()
                utils.QUOTE_INDEX_BY_NAME.clear()

                def reload():
                    loaded = utils.load_quote_index()
                    assert loaded == expected
                    assert len(report["groups"]) == calls_before, "Fresh disk cache unexpectedly queried upstream"
                    return dict(codes=len(loaded), extra_market_queries=0)
                check("fresh_cache_reload_without_network", reload)

                def concurrent_names():
                    utils.QUOTE_INDEX.clear()
                    utils.QUOTE_INDEX_BY_NAME.clear()
                    names = ["平安银行", "上证指数"] * 4
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        results = list(pool.map(utils.search_quote_from_index, names))
                    expected_ids = ["0.000001", "1.000001"] * 4
                    assert [[q.quote_id for q in values][0] if values else None for values in results] == expected_ids
                    assert len(report["groups"]) == calls_before
                    return dict(workers=8, extra_market_queries=0)
                check("concurrent_name_lookup_after_disk_reload", concurrent_names)
            else:
                for name in ("fresh_cache_reload_without_network", "concurrent_name_lookup_after_disk_reload"):
                    report["checks"].append(dict(name=name, status="SKIP", reason="No verified complete cache; no fallback used"))
    finally:
        stock_getter.get_realtime_quotes = original_quotes
        utils.QUOTE_INDEX_CACHE_PATH = old_path
        utils.QUOTE_INDEX.clear()
        utils.QUOTE_INDEX.update(original_index)
        utils.QUOTE_INDEX_BY_NAME.clear()
        utils.QUOTE_INDEX_BY_NAME.update(original_names)
        ef.configure(**config)
        report["original_cache_unchanged"] = fingerprint(user_path) == original_fingerprint
        report["summary"] = {status: sum(r["status"] == status for r in report["checks"])
                             for status in ("PASS", "FAIL", "SKIP")}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("complete_market_fetch", "codes", "duplicate_codes", "cache_written", "original_cache_unchanged", "summary")}), flush=True)
    return int(not report["complete_market_fetch"] or not report["original_cache_unchanged"]
               or report["summary"]["FAIL"] > 0 or report["summary"]["SKIP"] > 0)


if __name__ == "__main__":
    sys.exit(main())

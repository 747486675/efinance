"""Manual, bounded upstream acceptance checks; never imported by offline CI."""
import argparse
import json
import platform
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import efinance as ef
from efinance.query import normalize_query, query_one


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/online-acceptance-results.json"))
    parser.add_argument("--futures-id", help="Explicit current futures quote ID if market discovery is unavailable")
    args = parser.parse_args(argv)
    now = datetime.now(timezone(timedelta(hours=8)))
    report = dict(checked_at=now.isoformat(), python=platform.python_version(), checks=[])
    old_config = ef.get_config()
    ef.configure(connect_timeout=5, read_timeout=10, max_attempts=1, requests_per_second=2)
    report["config"] = ef.get_config()

    def check(asset, kind, function, code=None):
        record = dict(asset=asset, kind=kind, code=code)
        start = time.monotonic()
        try:
            value = function()
            record.update(rows=len(value), columns=list(value.columns))
            record["status"] = "PASS" if len(value) else "FAIL"
            if not len(value):
                record["reason"] = "No upstream records; not counted as verified"
            return value
        except Exception as error:
            record.update(status="FAIL", error_type=type(error).__name__, message=str(error),
                          attempts=getattr(error, "attempts", None))
            return None
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - start, 3)
            report["checks"].append(record)
            print("{} {}.{} {}".format(record["status"], asset, kind, code or ""), flush=True)

    def query(asset, kind, code):
        params = normalize_query(asset, [code], kind,
                                 **(dict(beg=(now - timedelta(days=30)).strftime("%Y%m%d"),
                                         end=now.strftime("%Y%m%d")) if kind == "history" else {}))
        return query_one(code, **{k: v for k, v in params.items() if k != "codes"})

    try:
        for asset, code in (("stock", "1.600519"), ("fund", "000001")):
            for kind in ("quote", "history"):
                check(asset, kind, lambda a=asset, k=kind, c=code: query(a, k, c), code)
        for asset, function in (("bond", ef.bond.get_realtime_quotes),
                                ("futures", ef.futures.get_realtime_quotes)):
            frame = check(asset, "market", function)
            code = None
            if frame is not None and not frame.empty and "行情ID" in frame:
                ids = frame["行情ID"].dropna()
                code = str(ids.iloc[0]) if len(ids) else None
            if asset == "futures" and args.futures_id:
                code = args.futures_id
            for kind in ("quote", "history"):
                if code:
                    check(asset, kind, lambda a=asset, k=kind, c=code: query(a, k, c), code)
                else:
                    report["checks"].append(dict(asset=asset, kind=kind, status="SKIP",
                                                 reason="Market query did not provide a current quote ID"))
                    print("SKIP {}.{} (no current quote ID)".format(asset, kind), flush=True)
    finally:
        ef.configure(**old_config)
        report["summary"] = {state: sum(c["status"] == state for c in report["checks"])
                             for state in ("PASS", "FAIL", "SKIP")}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]))
    return 1 if report["summary"]["FAIL"] or report["summary"]["SKIP"] else 0


if __name__ == "__main__":
    sys.exit(main())

"""Bounded repeated SDK checks for current quotes and trade details."""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import efinance as ef


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--output", type=Path, default=Path("docs/connectivity-acceptance-results.json"))
    args = parser.parse_args()
    old = ef.get_config()
    ef.configure(connect_timeout=5, read_timeout=10, max_attempts=2,
                 requests_per_second=1, max_concurrency=2)
    report = dict(checked_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                  rounds=args.rounds, config=ef.get_config(), checks=[])
    functions = [
        ("stock.latest", "1.600519", lambda: ef.stock.get_latest_quote("1.600519")),
        ("stock.deals", "1.600519", lambda: ef.stock.get_deal_detail("1.600519", max_count=30)),
        ("bond.deals", "0.128142", lambda: ef.bond.get_deal_detail("0.128142", max_count=30)),
        ("futures.deals", "113.rbm", lambda: ef.futures.get_deal_detail("113.rbm", max_count=30))]
    try:
        for number in range(1, args.rounds + 1):
            for name, code, function in functions:
                record = dict(name=name, round=number, code=code)
                started = time.monotonic()
                try:
                    frame = function()
                    record.update(status="PASS" if len(frame) else "EMPTY", rows=len(frame), columns=list(frame.columns))
                except Exception as error:
                    record.update(status="FAIL", error_type=type(error).__name__, message=str(error),
                                  attempts=getattr(error, "attempts", None))
                record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                report["checks"].append(record)
                print(record["status"], number, name, record.get("rows", ""), flush=True)
    finally:
        ef.configure(**old)
        report["summary"] = {state: sum(r["status"] == state for r in report["checks"])
                             for state in ("PASS", "FAIL", "EMPTY")}
        report["all_rounds_passed"] = all(r["status"] == "PASS" for r in report["checks"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]))
    return int(not report["all_rounds_passed"])


if __name__ == "__main__":
    sys.exit(main())

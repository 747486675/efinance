"""Manual follow-up for failed/empty upstream checks; no changes to product routing."""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import efinance as ef
from efinance.shared import session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bond-id", action="append", default=[], help="Explicit previously verified bond quote ID; repeat for two samples")
    parser.add_argument("--futures-id", default="113.rbm")
    parser.add_argument("--output", type=Path, default=Path("docs/upstream-followup-results.json"))
    args = parser.parse_args()
    old_config, old_trust = ef.get_config(), session.trust_env
    ef.configure(connect_timeout=5, read_timeout=10, max_attempts=1, requests_per_second=2)
    report = dict(checked_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                  checks=[], config=ef.get_config(),
                  environment_proxy_present=any(os.environ.get(key) for key in
                      ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")))

    def check(name, function, **context):
        record = dict(name=name, trust_env=session.trust_env, **context)
        start = time.monotonic()
        try:
            frame = function()
            record.update(status="PASS" if len(frame) else "EMPTY", rows=len(frame), columns=list(frame.columns))
            return frame
        except Exception as error:
            record.update(status="FAIL", error_type=type(error).__name__, message=str(error), attempts=getattr(error, "attempts", None))
            return None
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - start, 3)
            report["checks"].append(record)
            print(record["status"], name, flush=True)

    try:
        check("stock.latest", lambda: ef.stock.get_latest_quote("1.600519"))
        check("stock.deals", lambda: ef.stock.get_deal_detail("1.600519", max_count=30))
        check("futures.market", ef.futures.get_realtime_quotes)
        check("futures.history.explicit_id", lambda: ef.futures.get_quote_history(args.futures_id, beg="20261001", end="20261016"), quote_id=args.futures_id)
        check("futures.deals.explicit_id", lambda: ef.futures.get_deal_detail(args.futures_id, max_count=30), quote_id=args.futures_id)
        bonds = check("bond.market", ef.bond.get_realtime_quotes)
        codes = args.bond_id[:2]
        if not codes and bonds is not None and not bonds.empty:
            # A high-turnover sample avoids selecting the newest listing merely
            # because it leads the price-change ranking. Retain any empty flow.
            if "成交额" in bonds:
                order = pd.to_numeric(bonds["成交额"], errors="coerce").sort_values(ascending=False).index
                bonds = bonds.loc[order]
            column = next((c for c in ("行情ID", "债券代码", "代码") if c in bonds), None)
            if column:
                codes = list(bonds[column].dropna().astype(str).drop_duplicates().iloc[:2])
        if codes:
            for code in codes:
                check("bond.history", lambda c=code: ef.bond.get_quote_history(c, beg="20261001", end="20261016"), code=code)
                check("bond.today_flow", lambda c=code: ef.bond.get_today_bill(c), code=code)
                check("bond.history_flow", lambda c=code: ef.bond.get_history_bill(c), code=code)
                check("bond.deals", lambda c=code: ef.bond.get_deal_detail(c, max_count=30), code=code)
        else:
            report["checks"].append(dict(name="bond.followups", status="SKIP", reason="Market did not provide current samples"))
        # Diagnostic only: distinguish the process's configured proxy route from
        # a direct connection to the same existing source. Never persist this.
        session.trust_env = False
        check("diagnostic.direct.stock_latest", lambda: ef.stock.get_latest_quote("1.600519"))
        check("diagnostic.direct.futures_market", ef.futures.get_realtime_quotes)
    finally:
        session.trust_env = old_trust
        ef.configure(**old_config)
        report["summary"] = {status: sum(r["status"] == status for r in report["checks"])
                             for status in ("PASS", "FAIL", "EMPTY", "SKIP")}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]))
    return int(any(r["status"] != "PASS" for r in report["checks"]))


if __name__ == "__main__":
    sys.exit(main())

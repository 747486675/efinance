"""Manual latest-report verification, capped at one PDF in a temporary folder."""
import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import efinance as ef
from efinance.shared import session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", default="000001")
    parser.add_argument("--output", type=Path, default=Path("docs/pdf-download-acceptance-results.json"))
    args = parser.parse_args()
    old = ef.get_config()
    ef.configure(connect_timeout=5, read_timeout=10, max_attempts=1, requests_per_second=2)
    record = dict(checked_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                  function="fund.get_pdf_reports", fund_code=args.code, max_count=1, temporary_directory=True)
    try:
        listing = session.get("http://api.fund.eastmoney.com/f10/JJGG",
            headers={"Referer": "http://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
            params={"fundcode": args.code, "pageIndex": 1, "pageSize": 1, "type": 3}).json()["Data"]
        if not listing:
            raise RuntimeError("No report listing; latest selection is unverified")
        latest = listing[0]
        with tempfile.TemporaryDirectory() as folder:
            result = ef.fund.get_pdf_reports(args.code, max_count=1, save_dir=folder)
            files = list(Path(folder).rglob("*.pdf"))
            assert result is None
            if len(files) != 1:
                raise RuntimeError("Expected exactly one downloaded PDF")
            content = files[0].read_bytes()
            assert content.startswith(b"%PDF-"), "Downloaded body is not a PDF"
            assert files[0].name == latest["TITLE"] + ".pdf", "Report is not the listing head"
            record.update(status="PASS", files=1, bytes=len(content), filename=files[0].name,
                          published_at=latest.get("PUBLISHDATE"), matches_listing_head=True)
    except Exception as error:
        record.update(status="FAIL", error_type=type(error).__name__, message=str(error))
    finally:
        ef.configure(**old)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False))
    return int(record["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())

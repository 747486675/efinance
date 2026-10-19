"""Command-line entry point, compatible with Python 3.6."""
import argparse
import contextlib
import json
import os
import platform
import re
import sys
from pathlib import Path

from .batch import fetch
from .exceptions import EfinanceError, ParameterError
from .export import export_table
from .query import ASSETS, normalize_query, query_one, table_payload
from .settings import get_config


def _write_stdout(chunks):
    """Use UTF-8 for machine-readable output, regardless of locale."""
    binary_output = getattr(sys.stdout, "buffer", None)
    if binary_output is not None:
        sys.stdout.flush()
    for chunk in chunks:
        if binary_output is None:
            sys.stdout.write(chunk)
        else:
            binary_output.write(chunk.encode("utf-8"))
    if binary_output is not None:
        binary_output.flush()


def parser():
    root = argparse.ArgumentParser(prog="efinance", description="金融行情查询与导出")
    commands = root.add_subparsers(dest="command")
    for name in ("quote", "history"):
        cmd = commands.add_parser(name)
        cmd.add_argument("codes", nargs="+")
        cmd.add_argument("--asset", choices=ASSETS, default="stock")
        if name == "history":
            cmd.add_argument("--beg")
            cmd.add_argument("--end")
            cmd.add_argument("--klt", type=int)
            cmd.add_argument("--fqt", type=int)
        cmd.add_argument("--format", choices=("csv", "json", "xlsx", "parquet"), default="json")
        cmd.add_argument("--output", type=Path, help="单标的文件，或多标的输出目录")
        cmd.add_argument("--overwrite", action="store_true")
    commands.add_parser("doctor").add_argument("--online", action="store_true")
    web = commands.add_parser("web")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", default=8000, type=int)
    return root


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    try:
        if not args.command:
            root.print_help()
            return 0
        if args.command == "web":
            if sys.version_info < (3, 12):
                raise ParameterError("Web service requires Python 3.12+; core and CLI support Python 3.6+")
            try:
                from .web import serve
            except ImportError as cause:
                raise ParameterError("Install efinance[web] in a Python 3.12 environment") from cause
            serve(args.host, args.port)
            return 0
        if args.command == "doctor":
            report = dict(python=platform.python_version(), config=get_config(), online="not requested")
            if args.online:
                from .stock import get_latest_quote
                with contextlib.redirect_stdout(sys.stderr):
                    frame = get_latest_quote("1.600519")
                report["online"] = "passed" if not frame.empty else "empty response"
            _write_stdout([json.dumps(report, ensure_ascii=False, indent=2) + "\n"])
            return 1 if report["online"] == "empty response" else 0
        query = normalize_query(args.asset, args.codes, args.command, **{
            k: getattr(args, k, None) for k in ("beg", "end", "klt", "fqt")})
        if args.output is None and args.format in ("xlsx", "parquet"):
            raise ParameterError("binary formats require --output")
        # All argument and path validation precedes upstream work.
        if args.output:
            if len(query["codes"]) > 1:
                if args.output.exists() and not args.output.is_dir():
                    raise ParameterError("multiple codes require an output directory")
                filenames = [re.sub(r"[^\w.-]", "_", c) for c in query["codes"]]
                outputs = [args.output / (name + "." + args.format) for name in filenames]
                targets = outputs + [args.output / "failures.json"]
                # Include the manifest, and use the host filesystem's case rules.
                normalized = [os.path.normcase(os.path.abspath(str(path))) for path in targets]
                if len(set(normalized)) != len(normalized):
                    raise ParameterError("codes map to duplicate export filenames")
                if not args.overwrite and any(p.exists() for p in targets):
                    raise ParameterError("output exists; use --overwrite")
                args.output.mkdir(parents=True, exist_ok=True)
            else:
                if args.output.exists() and not args.overwrite:
                    raise ParameterError("output exists; use --overwrite")
                outputs = [args.output]
        kwargs = {key: value for key, value in query.items() if key != "codes"}
        with contextlib.redirect_stdout(sys.stderr):
            result = fetch(query_one, query["codes"], kwargs=kwargs)
        failures = dict(result.failures)
        csv_frames = []
        for i, code in enumerate(query["codes"]):
            if code not in result.successes:
                continue
            frame = result.successes[code]
            if args.output:
                try:
                    export_table(frame, outputs[i], args.format, args.overwrite)
                except (EfinanceError, OSError, ValueError, TypeError) as error:
                    failures[code] = dict(item=code, error_type=type(error).__name__,
                                          message=str(error), attempts=0,
                                          stage="export", path=str(outputs[i]))
            elif args.format == "csv":
                frame = frame.copy()
                if len(query["codes"]) > 1:
                    frame.insert(0, "查询标的", code)
                csv_frames.append(frame)
            else:
                _write_stdout([json.dumps(dict(code=code, **table_payload(frame, query)), ensure_ascii=False) + "\n"])
        if csv_frames:
            import pandas as pd
            from .export import csv_chunks
            _write_stdout(csv_chunks(pd.concat(csv_frames, ignore_index=True)))
        failure_list = [failures[code] for code in query["codes"] if code in failures]
        # stderr retains the failures even if the manifest itself cannot be written.
        if failure_list:
            print(json.dumps(failure_list, ensure_ascii=False), file=sys.stderr)
        if args.output and len(query["codes"]) > 1:
            with (args.output / "failures.json").open("w" if args.overwrite else "x", encoding="utf-8") as target:
                json.dump(failure_list, target, ensure_ascii=False, indent=2)
        if failure_list:
            return 2 if len(failure_list) == len(query["codes"]) and all(f["error_type"] == "ParameterError" for f in failure_list) else 1
        return 0
    except ParameterError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (EfinanceError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

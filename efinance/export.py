"""File exports with explicit overwrite protection. No persistent data store."""
import io
import pandas as pd
from pathlib import Path

from .exceptions import ParameterError


def csv_chunks(frame):
    yield "\ufeff"
    yield frame.iloc[:0].to_csv(index=False)
    for start in range(0, len(frame), 1000):
        yield frame.iloc[start:start + 1000].to_csv(index=False, header=False)


def export_table(frame, path, format=None, overwrite=False):
    path = Path(path)
    format = (format or path.suffix.lstrip(".")).lower()
    if format not in ("csv", "json", "xlsx", "parquet"):
        raise ParameterError("format must be csv, json, xlsx or parquet")
    if format == "csv":
        content = "".join(csv_chunks(frame)).encode("utf-8")
    elif format == "json":
        content = frame.to_json(orient="records", force_ascii=False, date_format="iso").encode("utf-8")
    else:
        buffer = io.BytesIO()
        try:
            if format == "xlsx":
                copy = frame.copy()
                for column in copy:
                    if "代码" in str(column) or column == "行情ID":
                        copy[column] = copy[column].map(lambda v: None if pd.isna(v) else str(v))
                copy.to_excel(buffer, index=False, engine="openpyxl")
            else:
                frame.to_parquet(buffer, index=False, engine="pyarrow")
        except ImportError as cause:
            raise ParameterError("Install efinance[{}] to export {}".format("excel" if format == "xlsx" else "parquet", format)) from cause
        content = buffer.getvalue()
    # Exclusive create avoids a check/write race and never truncates an existing file by default.
    with path.open("wb" if overwrite else "xb") as target:
        target.write(content)
    return path

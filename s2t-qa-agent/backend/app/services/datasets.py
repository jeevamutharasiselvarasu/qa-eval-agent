"""DatasetUploadService, ParserRegistry and DatasetProfiler.

Originals are stored unchanged; every dataset is normalized to Parquet for the engines. Processing uses
DuckDB/PyArrow (no full pandas load by default).
"""
from __future__ import annotations

import itertools
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import get_settings
from ..core.common import is_sensitive, mask_value, sha256_file, sha256_obj


class DatasetError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code  # UNSUPPORTED_FORMAT | EMPTY_FILE | CORRUPTED | TOO_LARGE | PARSE_FAILED


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@dataclass
class ParsedDataset:
    parquet_path: Path
    file_format: str
    sheet_name: str | None
    row_count: int
    columns: list[dict]


# ------------------------------------------------------------------ parsers
Parser = Callable[[Path, Path, dict], str | None]  # returns sheet name if applicable


def _con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute(f"SET memory_limit='{get_settings().sql_memory_limit}'")
    c.execute(f"SET threads={get_settings().sql_threads}")
    return c


def parse_csv(src: Path, out: Path, opts: dict) -> None:
    args = ["auto_detect=true", "sample_size=-1", "header=true"]
    if opts.get("delimiter"):
        args.append(f"delim={_lit(opts['delimiter'])}")
    if opts.get("all_varchar"):
        args.append("all_varchar=true")
    with _con() as c:
        c.execute(f"COPY (SELECT * FROM read_csv({_lit(src)}, {', '.join(args)})) TO {_lit(out)} (FORMAT parquet)")
        cols = [r[0] for r in c.execute(f"DESCRIBE SELECT * FROM read_parquet({_lit(out)})").fetchall()]
    if len(cols) == 1 and not opts.get("delimiter"):
        # the sniffer falls back to one column when rows are ragged; never accept that silently
        for d in (",", ";", "|", "\t"):
            if d in cols[0]:
                _raise_ragged(src, d)


def _raise_ragged(src: Path, delim: str) -> None:
    import csv as _csv
    bad = []
    with open(src, newline="", encoding="utf-8", errors="replace") as f:
        rows = _csv.reader(f, delimiter=delim)
        header = next(rows, [])
        for i, r in enumerate(rows, 2):
            if r and len(r) != len(header):
                bad.append(f"line {i} has {len(r)} fields")
            if len(bad) >= 5:
                break
    raise DatasetError("PARSE_FAILED", f"Rows have inconsistent column counts (header has {len(header)} fields, "
                       f"delimiter '{delim if delim != chr(9) else 'TAB'}'): " + "; ".join(bad or ["could not determine"]) +
                       ". Fix the file or set an explicit delimiter.")


def parse_json(src: Path, out: Path, opts: dict) -> None:
    with _con() as c:
        c.execute(f"COPY (SELECT * FROM read_json_auto({_lit(src)})) TO {_lit(out)} (FORMAT parquet)")


def parse_parquet(src: Path, out: Path, opts: dict) -> None:
    with open(src, "rb") as f:
        if f.read(4) != b"PAR1":
            raise DatasetError("CORRUPTED", "File is not a valid Parquet file")
    with _con() as c:
        c.execute(f"COPY (SELECT * FROM read_parquet({_lit(src)})) TO {_lit(out)} (FORMAT parquet)")


def parse_xlsx(src: Path, out: Path, opts: dict) -> str:
    import openpyxl
    with open(src, "rb") as f:
        if f.read(2) != b"PK":
            raise DatasetError("CORRUPTED", "File is not a valid XLSX workbook")
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    sheet = opts.get("sheet_name") or wb.sheetnames[0]
    if sheet not in wb.sheetnames:
        raise DatasetError("PARSE_FAILED", f"Sheet '{sheet}' not found; available: {wb.sheetnames}")
    ws = wb[sheet]
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if not header or all(h is None for h in header):
        raise DatasetError("EMPTY_FILE", "Sheet has no header row")
    names = []
    for i, h in enumerate(header):
        n = str(h).strip() if h is not None else f"column_{i+1}"
        while n in names:
            n += "_dup"
        names.append(n)
    writer = None
    batch: list[tuple] = []

    def flush():
        nonlocal writer, batch
        if not batch:
            return
        cols = list(zip(*batch))
        arrays = []
        for col in cols:
            vals = list(col)
            try:
                arrays.append(pa.array(vals))
            except (pa.ArrowInvalid, pa.ArrowTypeError):
                arrays.append(pa.array([None if v is None else str(v) for v in vals], pa.string()))
        tbl = pa.Table.from_arrays(arrays, names=names)
        if writer is None:
            writer = pq.ParquetWriter(out, tbl.schema)
        elif tbl.schema != writer.schema:
            tbl = tbl.cast(writer.schema, safe=False) if _castable(tbl.schema, writer.schema) else pa.Table.from_arrays(
                [pa.array([None if v is None else str(v) for v in c.to_pylist()], pa.string()) for c in tbl.columns], names=names)
        writer.write_table(tbl)
        batch = []

    for r in rows:
        if r is None or all(v is None for v in r):
            continue
        r = tuple(r[: len(names)]) + (None,) * max(0, len(names) - len(r))
        batch.append(r)
        if len(batch) >= 50_000:
            flush()
    # small files: infer types over all rows at once (best inference)
    flush()
    if writer is None:
        pq.write_table(pa.Table.from_arrays([pa.array([], pa.string()) for _ in names], names=names), out)
    else:
        writer.close()
    wb.close()
    return sheet


def _castable(a: pa.Schema, b: pa.Schema) -> bool:
    return all(x.type == y.type or pa.types.is_null(x.type) for x, y in zip(a, b))


PARSERS: dict[str, Callable] = {
    "csv": parse_csv,
    "txt": lambda s, o, opts: parse_csv(s, o, opts),
    "tsv": lambda s, o, opts: parse_csv(s, o, {**opts, "delimiter": opts.get("delimiter") or "\t"}),
    "json": parse_json,
    "ndjson": parse_json,
    "parquet": parse_parquet,
    "xlsx": parse_xlsx,
}


def parse_file(src: Path, out: Path, opts: dict | None = None) -> ParsedDataset:
    opts = opts or {}
    ext = src.suffix.lower().lstrip(".")
    if ext not in PARSERS:
        raise DatasetError("UNSUPPORTED_FORMAT", f"Unsupported file type '.{ext}'. Supported: {', '.join(sorted(PARSERS))}")
    size = src.stat().st_size
    if size == 0:
        raise DatasetError("EMPTY_FILE", "File is empty")
    if size > get_settings().max_upload_bytes:
        raise DatasetError("TOO_LARGE", "File exceeds the configured upload limit")
    try:
        sheet = PARSERS[ext](src, out, opts)
    except DatasetError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DatasetError("PARSE_FAILED", f"Could not parse {src.name}: {str(e)[:500]}") from e
    meta = pq.read_metadata(out)
    schema = pq.read_schema(out)
    cols = [{"name": f.name, "type": str(f.type)} for f in schema]
    if not cols:
        raise DatasetError("EMPTY_FILE", "No columns detected")
    return ParsedDataset(out, ext, sheet if isinstance(sheet, str) else None, meta.num_rows, cols)


def schema_fingerprint(columns: list[dict]) -> str:
    return sha256_obj([[c["name"], c["type"]] for c in columns])


def duck_columns(parquet_path: Path) -> list[dict]:
    with _con() as c:
        rows = c.execute(f"DESCRIBE SELECT * FROM read_parquet({_lit(parquet_path)})").fetchall()
    return [{"name": r[0], "type": r[1]} for r in rows]


# ------------------------------------------------------------------ profiler
NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE", "DECIMAL", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT")
TEMPORAL = ("DATE", "TIMESTAMP", "TIME")


def profile_parquet(path: Path, sample_size: int = 5, mask: bool = True) -> dict:
    with _con() as c:
        c.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet({_lit(path)})")
        cols = c.execute("DESCRIBE t").fetchall()
        n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        dup_rows = 0
        if cols and n:
            dup_rows = c.execute(
                f"SELECT COALESCE(SUM(cnt - 1), 0) FROM (SELECT COUNT(*) cnt FROM t GROUP BY ALL HAVING COUNT(*) > 1)"
                if False else
                f"SELECT COALESCE(SUM(cnt - 1), 0) FROM (SELECT COUNT(*) cnt FROM t GROUP BY {', '.join(_q(x[0]) for x in cols)} HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        out_cols = []
        for name, typ, *_ in cols:
            q = _q(name)
            base = typ.split("(")[0].upper()
            is_num = base in NUMERIC
            is_tmp = any(base.startswith(t) for t in TEMPORAL)
            is_str = base == "VARCHAR"
            stats = c.execute(
                f"SELECT COUNT(*) - COUNT({q}), COUNT(DISTINCT {q}), "
                f"{'SUM(CASE WHEN TRIM(' + q + ') = ' + chr(39) + chr(39) + ' THEN 1 ELSE 0 END)' if is_str else '0'}, "
                f"{'MIN(' + q + ')::VARCHAR, MAX(' + q + ')::VARCHAR' if (is_num or is_tmp or is_str) else 'NULL, NULL'}, "
                f"{'AVG(' + q + ')::DOUBLE, STDDEV_SAMP(' + q + ')::DOUBLE, SUM(CASE WHEN ' + q + ' < 0 THEN 1 ELSE 0 END), SUM(CASE WHEN ' + q + ' = 0 THEN 1 ELSE 0 END)' if is_num else 'NULL, NULL, NULL, NULL'}, "
                f"{'MAX(LENGTH(' + q + '))' if is_str else 'NULL'} FROM t"
            ).fetchone()
            nulls, distinct, blanks, mn, mx, mean, std, neg, zero, maxlen = stats
            dup_vals = c.execute(f"SELECT COALESCE(SUM(cnt - 1),0) FROM (SELECT COUNT(*) cnt FROM t WHERE {q} IS NOT NULL GROUP BY {q} HAVING COUNT(*)>1)").fetchone()[0]
            samples = [r[0] for r in c.execute(f"SELECT DISTINCT {q}::VARCHAR FROM t WHERE {q} IS NOT NULL ORDER BY 1 LIMIT {sample_size}").fetchall()]
            sensitive = is_sensitive(name)
            if mask and sensitive:
                samples = [mask_value(s) for s in samples]
                mn = mask_value(mn) if mn is not None else None
                mx = mask_value(mx) if mx is not None else None
            col = {
                "name": name, "type": typ, "null_count": int(nulls), "null_pct": round(100 * nulls / n, 2) if n else 0.0,
                "blank_count": int(blanks or 0), "distinct_count": int(distinct), "duplicate_value_count": int(dup_vals),
                "min": mn, "max": mx, "sample_values": samples, "sensitive": sensitive,
            }
            if is_num:
                col.update({"mean": mean, "stddev": std, "negative_count": int(neg or 0), "zero_count": int(zero or 0)})
            if is_str:
                col["max_length"] = maxlen
                col["format_issues"] = _string_format_issues(c, q, n - nulls)
            out_cols.append(col)
        keys = _candidate_keys(c, out_cols, n)
    columns_meta = [{"name": x["name"], "type": x["type"]} for x in out_cols]
    return {"row_count": int(n), "column_count": len(out_cols), "duplicate_row_count": int(dup_rows),
            "columns": out_cols, "candidate_keys": keys, "schema_fingerprint": schema_fingerprint(columns_meta)}


def _string_format_issues(c, q: str, nonnull: int) -> dict:
    """Detect columns that look numeric/date-like but contain non-conforming values."""
    if not nonnull:
        return {}
    num_ok, date_ok, ws = c.execute(
        f"SELECT SUM(CASE WHEN TRY_CAST({q} AS DOUBLE) IS NOT NULL THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN TRY_CAST({q} AS DATE) IS NOT NULL THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN {q} <> TRIM({q}) THEN 1 ELSE 0 END) FROM t WHERE {q} IS NOT NULL").fetchone()
    issues = {}
    if ws:
        issues["leading_trailing_whitespace"] = int(ws)
    if num_ok and num_ok / nonnull >= 0.8 and num_ok < nonnull:
        issues["non_numeric_in_numeric_like"] = int(nonnull - num_ok)
    if date_ok and date_ok / nonnull >= 0.8 and date_ok < nonnull:
        issues["invalid_dates_in_date_like"] = int(nonnull - date_ok)
    return issues


def _candidate_keys(c, cols: list[dict], n: int) -> list[list[str]]:
    if n == 0:
        return []
    single = [[x["name"]] for x in cols if x["null_count"] == 0 and x["distinct_count"] == n]
    if single:
        return single[:5]
    pool = [x["name"] for x in cols if x["null_count"] == 0 and x["distinct_count"] > 1][:10]
    found = []
    for a, b in itertools.combinations(pool, 2):
        d = c.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT {_q(a)}, {_q(b)} FROM t)").fetchone()[0]
        if d == n:
            found.append([a, b])
            if len(found) >= 3:
                break
    return found


def store_upload(src_tmp: Path, run_storage: Path, filename: str) -> tuple[Path, str, int]:
    dest = run_storage / filename
    i = 1
    while dest.exists():
        dest = run_storage / f"{Path(filename).stem}_{i}{Path(filename).suffix}"
        i += 1
    shutil.move(str(src_tmp), dest)
    dest.chmod(0o440)  # originals are read-only
    return dest, sha256_file(dest), dest.stat().st_size


def preview_rows(path: Path, limit: int = 50, mask: bool = True) -> dict:
    with _con() as c:
        rel = c.execute(f"SELECT * FROM read_parquet({_lit(path)}) LIMIT {int(limit)}")
        names = [d[0] for d in rel.description]
        rows = rel.fetchall()
    recs = [{k: _jsonable(v) for k, v in zip(names, r)} for r in rows]
    if mask:
        recs = [{k: (mask_value(v) if is_sensitive(k) else v) for k, v in r.items()} for r in recs]
    return {"columns": names, "rows": recs}


def _jsonable(v):
    import datetime
    import decimal
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return v

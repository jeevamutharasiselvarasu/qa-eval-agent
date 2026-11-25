"""Source and target files, profiling and column rules (mappings)."""

from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path

import duckdb
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...config import get_settings
from ...core.common import audit, mask_records, sha256_file
from ...models import (
    ColumnMapping,
    Dataset,
    DatasetProfile,
)
from ...services import blob
from ...services import runs as run_svc
from ...services.datasets import preview_rows
from ..deps import DB, Analyst, Viewer, after_change, get_run, row, wf

router = APIRouter()


def _store(db, run, p, tmp: Path, name: str, role: str, label: str, alias: str, opts: dict, source_uri: str | None = None):
    try:
        return run_svc.upload_dataset(db, run, tmp, name, role, p.user, label, alias, opts, source_uri)
    except run_svc.WorkflowError as e:
        tmp.unlink(missing_ok=True)
        db.commit()
        raise HTTPException(e.status, str(e)) from e


def _upload(run_id, role, file: UploadFile, label, alias, delimiter, sheet_name, db, p):
    run = get_run(db, run_id, p)
    s = get_settings()
    with tempfile.NamedTemporaryFile(delete=False, dir=s.storage_dir) as tmp:
        size = 0
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > s.max_upload_bytes:
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(413, "File exceeds the configured upload limit")
            tmp.write(chunk)
    opts = {k: v for k, v in {"delimiter": delimiter, "sheet_name": sheet_name}.items() if v}
    return row(_store(db, run, p, Path(tmp.name), file.filename or "upload", role, label, alias, opts))


# ------------------------------------------------------------------ Azure Blob / ADLS Gen2 paths
class BlobPath(BaseModel):
    path: str = Field(max_length=2000)


class BlobDataset(BlobPath):
    label: str = Field(default="", max_length=200)
    alias: str = Field(default="", max_length=64)
    delimiter: str = Field(default="", max_length=4)
    sheet_name: str = Field(default="", max_length=128)


def _blob_call(fn, *a):
    try:
        return fn(*a)
    except blob.BlobError as e:
        raise HTTPException(e.status, str(e)) from e


def _from_blob(run_id: str, role: str, body: BlobDataset, db, p):
    run = get_run(db, run_id, p)
    opts = {k: v for k, v in {"delimiter": body.delimiter, "sheet_name": body.sheet_name}.items() if v}
    tmp, name, ref, listing = _blob_call(blob.fetch, body.path, get_settings().storage_dir, opts)
    audit(db, p.user, "DATASET_READ_FROM_STORAGE", run_id, "QA_RUN", run_id, uri=ref.uri, files=len(listing["files"]))
    ds = _store(db, run, p, tmp, name, role, body.label or ref.name, body.alias if role == "SOURCE" else "", opts, ref.uri)
    return row(ds)


@router.post("/storage/check")
def storage_check(body: BlobPath, p: Analyst):
    """List what a storage path points to (one file or a folder of part files) without reading the data."""
    return _blob_call(blob.check, body.path)


@router.post("/runs/{run_id}/datasets/sources/blob")
def source_from_blob(run_id: str, body: BlobDataset, db: DB, p: Analyst):
    return _from_blob(run_id, "SOURCE", body, db, p)


@router.post("/runs/{run_id}/datasets/target/blob")
def target_from_blob(run_id: str, body: BlobDataset, db: DB, p: Analyst):
    return _from_blob(run_id, "TARGET", body, db, p)


@router.post("/runs/{run_id}/datasets/{ds_id}/reload")
def reload_from_blob(run_id: str, ds_id: str, db: DB, p: Analyst):
    """Read a dataset again from the storage path it came from. Nothing changes if the data is the same."""
    run = get_run(db, run_id, p)
    d = db.get(Dataset, ds_id)
    if not d or d.run_id != run_id or d.role not in ("SOURCE", "TARGET"):
        raise HTTPException(404, "Dataset not found")
    if not d.source_uri:
        raise HTTPException(400, "This dataset was uploaded as a file; upload the new file instead")
    opts = {"sheet_name": d.sheet_name} if d.sheet_name else {}
    tmp, name, ref, listing = _blob_call(blob.fetch, d.source_uri, get_settings().storage_dir, opts)
    if sha256_file(tmp) == d.fingerprint:
        tmp.unlink(missing_ok=True)
        return {"unchanged": True, "dataset": row(d)}
    role, alias, label, feeds = d.role, d.alias, d.label, d.feeds_target
    try:
        if role == "SOURCE":  # same alias, so the SQL and the rules keep working
            db.delete(d)
            db.flush()
        new = run_svc.upload_dataset(db, run, tmp, name, role, p.user, label, alias if role == "SOURCE" else "", opts, ref.uri)
    except run_svc.WorkflowError as e:
        db.rollback()  # keep the dataset that was there
        tmp.unlink(missing_ok=True)
        raise HTTPException(e.status, f"The data at {ref.uri} could not be read, so the previous version is kept. {e}") from e
    audit(db, p.user, "DATASET_RELOADED", run_id, "DATASET", new.id, previous=ds_id, uri=ref.uri, files=len(listing["files"]))
    new.feeds_target = feeds
    db.flush()
    if db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)).first():
        after_change(db, run, p.user)
    return {"unchanged": False, "dataset": row(new)}


@router.post("/runs/{run_id}/datasets/sources")
def upload_source(
    run_id: str,
    db: DB,
    p: Analyst,
    file: UploadFile = File(...),
    label: str = Form(""),
    alias: str = Form(""),
    delimiter: str = Form(""),
    sheet_name: str = Form(""),
):
    return _upload(run_id, "SOURCE", file, label, alias, delimiter, sheet_name, db, p)


@router.post("/runs/{run_id}/datasets/target")
def upload_target(
    run_id: str,
    db: DB,
    p: Analyst,
    file: UploadFile = File(...),
    label: str = Form(""),
    delimiter: str = Form(""),
    sheet_name: str = Form(""),
):
    return _upload(run_id, "TARGET", file, label, "", delimiter, sheet_name, db, p)


@router.get("/runs/{run_id}/datasets")
def datasets(run_id: str, db: DB, p: Viewer, track: str = ""):
    get_run(db, run_id, p)
    stmt = select(Dataset).where(Dataset.run_id == run_id).order_by(Dataset.created_at)
    if track:
        stmt = stmt.where(Dataset.track == track)
    return [row(d) for d in db.scalars(stmt)]


class DatasetPatch(BaseModel):
    feeds_target: bool | None = None
    label: str | None = Field(default=None, max_length=200)


@router.patch("/runs/{run_id}/datasets/{ds_id}")
def patch_dataset(run_id: str, ds_id: str, body: DatasetPatch, db: DB, p: Analyst):
    get_run(db, run_id, p)
    d = db.get(Dataset, ds_id)
    if not d or d.run_id != run_id or d.role != "SOURCE":
        raise HTTPException(404, "Source dataset not found")
    if body.feeds_target is not None:
        d.feeds_target = body.feeds_target
    if body.label is not None:
        d.label = body.label
    audit(db, p.user, "DATASET_UPDATED", run_id, "DATASET", ds_id, feeds_target=d.feeds_target, label=d.label)
    db.flush()
    if body.feeds_target is not None and db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)).first():
        after_change(db, get_run(db, run_id, p), p.user)
    return row(d)


@router.delete("/runs/{run_id}/datasets/{ds_id}")
def delete_dataset(run_id: str, ds_id: str, db: DB, p: Analyst):
    get_run(db, run_id, p)
    d = db.get(Dataset, ds_id)
    if not d or d.run_id != run_id or d.role not in ("SOURCE", "TARGET"):
        raise HTTPException(404, "Dataset not found")
    db.delete(d)
    audit(db, p.user, "DATASET_REMOVED", run_id, "DATASET", ds_id, role=d.role, fingerprint=d.fingerprint)
    db.flush()
    if db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)).first():
        after_change(db, get_run(db, run_id, p), p.user)
    return {"deleted": ds_id}


@router.get("/runs/{run_id}/datasets/{ds_id}/preview")
def dataset_preview(run_id: str, ds_id: str, db: DB, p: Viewer, limit: int = Query(50, le=500)):
    get_run(db, run_id, p)
    d = db.get(Dataset, ds_id)
    if not d or d.run_id != run_id:
        raise HTTPException(404, "Dataset not found")
    return preview_rows(Path(d.parquet_path), limit)


@router.get("/runs/{run_id}/datasets/{ds_id}/download")
def dataset_download(run_id: str, ds_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    d = db.get(Dataset, ds_id)
    if not d or d.run_id != run_id or d.role in ("SOURCE", "TARGET"):
        raise HTTPException(404, "Only generated datasets can be downloaded")
    with duckdb.connect(":memory:") as c:
        cur = c.execute("SELECT * FROM read_parquet(?)", [d.parquet_path])
        names = [x[0] for x in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=names)
    w.writeheader()
    for r in mask_records(rows):
        w.writerow(r)
    name = f"{d.role.lower()}_{d.alias}.csv"
    audit(db, p.user, "DATASET_DOWNLOADED", run_id, "DATASET", ds_id)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@router.post("/runs/{run_id}/profile")
def profile(run_id: str, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    try:
        profs = run_svc.profile_all(db, run, p.user)
    except run_svc.WorkflowError as e:
        db.commit()
        raise HTTPException(e.status, str(e)) from e
    if db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)).first():
        after_change(db, run, p.user)
    return [row(x) for x in profs]


@router.get("/runs/{run_id}/profiles")
def profiles(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    out = []
    for d in db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role.in_(["SOURCE", "TARGET"]))):
        pr = db.scalars(select(DatasetProfile).where(DatasetProfile.dataset_id == d.id).order_by(DatasetProfile.created_at.desc())).first()
        out.append({"dataset": row(d), "profile": pr.profile if pr else None})
    return out


@router.post("/runs/{run_id}/mappings/generate")
def mappings_generate(run_id: str, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    return [row(m) for m in wf(run_svc.generate_mappings, db, run, p.user)]


@router.get("/runs/{run_id}/mappings")
def mappings(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    return [row(m) for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id).order_by(ColumnMapping.id))]

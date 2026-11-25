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
from ...core.common import audit, mask_records
from ...models import (
    ColumnMapping,
    Dataset,
    DatasetProfile,
)
from ...services import runs as run_svc
from ...services.datasets import preview_rows
from ..deps import DB, Analyst, Viewer, after_change, get_run, row, wf

router = APIRouter()


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
    try:
        ds = run_svc.upload_dataset(db, run, Path(tmp.name), file.filename or "upload", role, p.user, label, alias, opts)
    except run_svc.WorkflowError as e:
        Path(tmp.name).unlink(missing_ok=True)
        db.commit()
        raise HTTPException(e.status, str(e)) from e
    return row(ds)


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

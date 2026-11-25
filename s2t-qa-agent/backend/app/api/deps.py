"""Shared API dependencies and helpers.

Database sessions use ``scope="function"`` so each request commits BEFORE its response is sent: a follow-up request
from the browser must see the data the previous response reported.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import Principal, get_principal, require
from ..db import get_db
from ..models import QARun
from ..services import execution as exec_svc
from ..services import runs as run_svc
from ..services import target_spec
from ..services.jira import JiraError

DB = Annotated[Session, Depends(get_db, scope="function")]
CurrentUser = Annotated[Principal, Depends(get_principal)]
Viewer = Annotated[Principal, Depends(require("viewer"))]
Analyst = Annotated[Principal, Depends(require("analyst"))]
Approver = Annotated[Principal, Depends(require("approver"))]
Admin = Annotated[Principal, Depends(require("admin"))]

HIDDEN = {"original_path", "parquet_path", "path"}  # server-side file paths are never returned


def row(obj, extra: dict | None = None) -> dict:
    """A model row as a JSON-able dict (without server-side paths)."""
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in HIDDEN}
    if extra:
        d.update(extra)
    return d


def get_run(db: Session, run_id: str, p: Principal) -> QARun:
    """The run, if it exists and the caller may access its project (404 otherwise, so existence is not leaked)."""
    run = db.get(QARun, run_id)
    if not run or not p.can_access(run.project):
        raise HTTPException(404, "Run not found")
    return run


def wf(fn, *a, **kw):
    """Call a service function, turning its business errors into HTTP errors."""
    try:
        return fn(*a, **kw)
    except run_svc.WorkflowError as e:
        raise HTTPException(e.status, str(e)) from e
    except exec_svc.ExecutionError as e:
        raise HTTPException(e.status, str(e)) from e
    except JiraError as e:
        raise HTTPException(400, f"{e.code}: {e}") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def after_change(db: Session, run: QARun, actor: str) -> None:
    """Re-derive the column rules and, when everything is clear, (re)generate the SQL and the test pack."""
    target_spec.refresh(db, run, actor)
    target_spec.sync_sql(db, run, actor)


class DecisionBody(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(default="", max_length=4000)


class ExecBody(BaseModel):
    force: bool = False

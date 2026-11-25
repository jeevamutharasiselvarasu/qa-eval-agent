"""REST API. Every run-scoped endpoint checks the caller's role and project access (see ``deps.get_run``)."""

from fastapi import APIRouter

from .routers import datasets, executions, overview, reports, requirements, runs, testing, transformations

router = APIRouter(prefix="/api")
for _module in (overview, runs, requirements, datasets, transformations, testing, executions, reports):
    router.include_router(_module.router)

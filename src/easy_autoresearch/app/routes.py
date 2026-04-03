"""FastAPI routes for the observability dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from easy_autoresearch.app.viewmodels import build_dashboard_context
from easy_autoresearch.storage.queries import latest_session, session_snapshot


def build_router(*, repo_path: Path, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/api/session/current")
    def current_session() -> JSONResponse:
        session = latest_session(repo_path)
        if session is None:
            return JSONResponse({"session": None, "experiments": [], "activities": []})
        return JSONResponse(session_snapshot(repo_path, int(session["id"])))

    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        selected_experiment_id = request.query_params.get("experiment_id")
        selected_run_id = request.query_params.get("run_id")
        session = latest_session(repo_path)
        if session is None:
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                {
                    "request": request,
                    "repo_path": str(repo_path),
                    "session": None,
                    "experiments": [],
                    "activities": [],
                    "active_phase": None,
                    "selected_experiment": None,
                    "selected_experiment_id": None,
                    "selected_run": None,
                    "selected_run_id": None,
                    "selected_run_activities": [],
                },
            )
        try:
            snapshot = session_snapshot(repo_path, int(session["id"]))
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        context = build_dashboard_context(
            snapshot,
            selected_experiment_id=(
                int(selected_experiment_id)
                if selected_experiment_id and selected_experiment_id.isdigit()
                else None
            ),
            selected_run_id=(
                int(selected_run_id)
                if selected_run_id and selected_run_id.isdigit()
                else None
            ),
        )
        context["repo_path"] = str(repo_path)
        context["request"] = request
        return templates.TemplateResponse(request, "dashboard.html", context)

    return router

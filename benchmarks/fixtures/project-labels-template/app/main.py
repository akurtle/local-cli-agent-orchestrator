from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.database import Database


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
MIGRATIONS = ROOT / "migrations"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


def create_app(database_path: str | Path | None = None) -> FastAPI:
    path = Path(
        database_path
        or os.environ.get("PROJECT_TRACKER_DB", ROOT / "data" / "projects.db")
    )
    database = Database(path, MIGRATIONS)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.initialize()
        yield

    application = FastAPI(title="Project Tracker", lifespan=lifespan)
    application.state.database = database
    application.mount("/static", StaticFiles(directory=STATIC), name="static")

    @application.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @application.get("/api/projects")
    def list_projects() -> list[dict]:
        return database.list_projects()

    @application.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def create_project(payload: ProjectCreate) -> dict:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Project name cannot be blank")
        return database.create_project(name, payload.description.strip())

    return application


app = create_app()

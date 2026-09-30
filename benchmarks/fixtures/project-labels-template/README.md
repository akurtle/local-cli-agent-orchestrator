# Project Tracker

A deliberately small FastAPI application used to benchmark coding-agent
workflows. It has a SQLite-backed project API, a browser dashboard, migrations,
and tests. Project labels are intentionally absent from the starting state.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000> to use the dashboard.

## API

### List projects

```http
GET /api/projects
```

### Create a project

```http
POST /api/projects
Content-Type: application/json

{"name": "Benchmark", "description": "Measure coding workflows"}
```

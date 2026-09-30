from fastapi.testclient import TestClient

from app.main import create_app


def test_project_lifecycle(tmp_path):
    app = create_app(tmp_path / "projects.db")
    with TestClient(app) as client:
        assert client.get("/api/projects").json() == []

        response = client.post(
            "/api/projects",
            json={"name": "Benchmark", "description": "Compare workflows"},
        )
        assert response.status_code == 201
        assert response.json()["name"] == "Benchmark"

        projects = client.get("/api/projects").json()
        assert len(projects) == 1
        assert projects[0]["description"] == "Compare workflows"


def test_project_name_cannot_be_blank(tmp_path):
    app = create_app(tmp_path / "projects.db")
    with TestClient(app) as client:
        response = client.post("/api/projects", json={"name": "   "})
    assert response.status_code == 422

from fastapi.testclient import TestClient

from app.main import create_app


def create_project(client: TestClient) -> int:
    response = client.post("/api/projects", json={"name": "Benchmark"})
    assert response.status_code == 201
    return int(response.json()["id"])


def test_labels_can_be_created_and_listed(tmp_path):
    app = create_app(tmp_path / "labels.db")
    with TestClient(app) as client:
        project_id = create_project(client)
        response = client.post(
            f"/api/projects/{project_id}/labels", json={"name": "backend"}
        )
        assert response.status_code == 201
        assert response.json()["name"] == "backend"
        assert response.json()["project_id"] == project_id

        labels = client.get(f"/api/projects/{project_id}/labels")
        assert labels.status_code == 200
        assert [label["name"] for label in labels.json()] == ["backend"]


def test_label_validation_and_missing_projects(tmp_path):
    app = create_app(tmp_path / "labels.db")
    with TestClient(app) as client:
        project_id = create_project(client)
        assert client.post(
            f"/api/projects/{project_id}/labels", json={"name": ""}
        ).status_code == 422
        assert client.post(
            f"/api/projects/{project_id}/labels", json={"name": "x" * 31}
        ).status_code == 422
        assert client.post(
            "/api/projects/9999/labels", json={"name": "unknown"}
        ).status_code == 404

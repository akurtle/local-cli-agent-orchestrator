# Project labels

Add project labels to the benchmark application.

Requirements:

- Add a `labels` table through a new migration. Each label belongs to a project.
- Add `POST /api/projects/{id}/labels` and `GET /api/projects/{id}/labels`.
- Validate that label names contain between 1 and 30 characters.
- Return `404` when adding a label to a project that does not exist.
- Display each project's labels in the browser dashboard.
- Add backend and frontend tests.
- Update the API documentation.

Completion checks:

- `python -m pytest -q`

# Project labels

Add project labels to the benchmark application.

Requirements:

- Add a label table and database migration.
- Add `POST /projects/{id}/labels`.
- Validate that label names contain between 1 and 30 characters.
- Display labels in the React project view.
- Add backend and frontend tests.
- Update the API documentation.

Completion checks:

- `python -m pytest -q`
- `npm test -- --run`
- `npm run typecheck`

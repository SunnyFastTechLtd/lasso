# Contributing to Lasso

Thanks for taking the time to contribute. This guide covers the local workflow, coding
conventions and the review process.

## Before you start

- Search existing issues and pull requests first — the change may already be in progress.
- For anything larger than a typo or a one-line fix, open an issue and describe the problem
  before writing code. It avoids work in a direction the maintainers cannot merge.
- Security issues must **not** be filed publicly; follow [SECURITY.md](SECURITY.md).

## Development setup

Backend:

```bash
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

Frontend:

```bash
cd frontend
npm ci
npm run dev
```

## Checks to run before opening a PR

```bash
# backend
python manage.py test backend

# frontend
cd frontend
npm run lint
npm run build
```

If you touched models, include the migration (`python manage.py makemigrations`) and make sure
`python manage.py makemigrations --check` reports no changes.

## Code conventions

- **Python**: keep the existing app layout (`backend/apps/<app>/`); put business rules in
  `services.py`, keep views thin. Chinese docstrings and comments follow the existing style.
- **TypeScript/React**: function components and hooks only; data access goes through
  `frontend/src/api/`; no new global state without a reason.
- **UI text**: user-facing strings are Chinese, consistently with the product.
- **Errors**: never swallow exceptions silently — return an explicit message the UI can show.
- **Secrets**: never commit real keys, tokens, internal hostnames or customer data. Use
  `.env.example` for new configuration, with placeholder values only.

## Commit messages

Short imperative subject, optional body explaining *why*. Examples:

```
feat(agents): support cross-environment export with approval
fix(ui): stop sending expired token to the login endpoint
docs: rewrite deployment guide for PostgreSQL
```

Group unrelated changes into separate commits.

## Pull requests

- Keep the PR focused; one concern per PR.
- Describe the problem, the approach and how you verified it. Screenshots help for UI changes.
- Link the related issue (`Closes #123`).
- Add or update tests when behaviour changes.
- A maintainer will review; please respond to review comments rather than force-pushing over
  them when possible.

## License of contributions

This project is licensed under the Apache License, Version 2.0. By submitting a pull request you
agree that your contribution is licensed under the same terms and that you have the right to
submit it.

# Contributing to MH-Dowsample

Thank you for helping improve MH-Dowsample. Keep changes focused, testable, and safe for users who
manage valuable audio libraries.

## Development setup

1. Fork or clone the repository.
2. Create and activate a Python 3.11+ virtual environment.
3. Install `requirements.txt` and `requirements-dev.txt`.
4. Install Playwright Chromium with `python -m playwright install chromium --only-shell`.
5. Copy `.env.example` to `.env`; never commit the resulting `.env` file.

## Working on a change

- Create a short branch name that describes one change.
- Preserve source audio unless a test explicitly verifies move behavior.
- Keep network tests deterministic and mock external services.
- Add or update tests whenever behavior changes.
- Avoid committing audio, databases, browser profiles, logs, credentials, or generated output.

## Required checks

Run the same core checks used by CI before opening a pull request:

```powershell
python -m pytest tests -v --cov=. --cov-fail-under=68
python -m ruff check .
python -m mypy config.py exceptions.py quality_gate.py processor.py organizer.py organize.py crawler.py bot.py utils tools --ignore-missing-imports
python -m bandit -r . -x ./tests,./tools -ll
```

## Pull requests

A pull request should explain:

- what changed and why;
- the effect on users and existing libraries;
- the root cause when fixing a defect;
- the checks used to validate the change.

Keep unrelated cleanup out of the same pull request. Do not include sample packs or any audio you do
not have permission to redistribute.

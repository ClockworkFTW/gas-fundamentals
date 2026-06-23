# gas-fundamentals — Claude Code instructions

Full project context — architecture, data sources, access approach, and the
normalized output schema — is in **README.md**. Read it first.

## Python environment (important)

- This project targets **Python 3.11** to match the Jenkins agent (3.11.9).
- The virtual environment lives at `.venv` in the project root.
- Run **all** Python and pip commands through that venv's interpreter, e.g.:
  - `.\.venv\Scripts\python.exe <script.py>`
  - `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
  (Windows venv uses `Scripts\`, not `bin/`.)
- Do **not** use the system Python (3.14), and do **not** use any 3.12+ syntax —
  the code must run on 3.11.9.

## Conventions

- Secrets (Power Automate trigger URL, EIA API key) load from `.env` via
  `python-dotenv`. Never hardcode or commit them.
- All ingestion is `requests`-based (GET, or `requests.Session` for WebForms
  POST). No headless browser / Puppeteer.
- Every EBB module emits the common normalized record shape in README §5.
- Gas day is Pacific time — be explicit about timezones everywhere; store
  `pulled_at` in UTC.

## First task

Build `src/ebb/pipe_ranger.py` as described in README §8. Capture Pipe Ranger's
real download request (browser devtools → Network) before implementing, so it
runs against the actual URL and parameters.
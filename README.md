# ContextLens 3.0

A local workspace for exploring UnvibeCode repository-analysis results. ContextLens turns a generated repository-context export into searchable code chunks, file relationships, unresolved references, context exports, and saved investigation notes.

Built as a prototype for the UnvibeCode Engineering Challenge 2026 — Cohort 3, Track 2 (Developer Adoption & Repeat Usage).

## Features

- Analyze a public GitHub repository with the optional UnvibeCode CLI workflow.
- Import an existing UnvibeCode context ZIP.
- Search indexed code by text, file, language, symbol, or entity type.
- Inspect code chunks and explore a bounded relationship graph.
- Review unresolved relationships as investigation leads, not confirmed bugs.
- Export file context in Markdown or JSON.
- Save and reopen investigation notes.
- Preview generated HTML reports when included in an imported analysis.
- Explore the bundled compact Evidently sample context.

## Quick start

Requirements: Python 3.10+, Windows/macOS/Linux. Git is needed only for the optional live repository-analysis workflow.

**Windows:** Run `run_windows.bat`, allow first-time dependency installation, then open http://127.0.0.1:7863.

**macOS/Linux:**
```bash
chmod +x run_mac_linux.sh
./run_mac_linux.sh
```
Then open http://127.0.0.1:7863.

Choose **Explore sample analysis** to try the bundled fixture, or import your own UnvibeCode context ZIP.

## Optional live analysis

Install Git and the UnvibeCode CLI in the same Python environment:
```bash
python -m pip install --upgrade unvibecode
python -m unvibecode --help
```
Then use **Analyze a repository** and enter a public GitHub HTTPS URL. This path depends on the installed CLI version and may require provider credentials, quotas, or paid usage. ZIP import and the bundled sample work independently. See [OPTIONAL_UNVIBECODE.md](OPTIONAL_UNVIBECODE.md).

## Project files

The application is implemented in `server.py` (FastAPI routes), `contextlens_app.py` (context loading, search, graph and export logic), `static/` (JavaScript and CSS), and `templates/` (HTML). Test suites are `contextlens_tests.py` and `web_tests.py`. See [QA_REPORT.md](QA_REPORT.md) for checks and limitations.

## Tests

```bash
python -m unittest -v contextlens_tests web_tests
node --check static/app.js
python -m compileall -q server.py contextlens_app.py contextlens_tests.py web_tests.py
```

## Security and limitations

- ContextLens binds to 127.0.0.1 and is intended for local, single-user use.
- Do not expose it directly to the public internet without authentication, per-user storage isolation, rate limiting, CSRF protection, and deployment hardening.
- ZIP imports validate required files and reject path traversal, symlinks, oversized members, and excessive uncompressed size.
- Do not import private source code, credentials, secrets, or customer data without permission.
- Relationship matches and unresolved records are suggestions; verify conclusions against source code.
- The optional live UnvibeCode workflow has not been verified end to end in the published build environment.

# ContextLens 3.0 — local QA report

## Verification performed in the rebuild environment

- Python compilation: `compileall` passed for the backend and test modules.
- JavaScript syntax: `node --check static/app.js` passed.
- Engine + API regression suite: **31 tests passed** (19 engine tests + 12 website API tests).
- Sample import: the bundled compact Evidently fixture loads successfully; exact fixture counts are recorded in its `manifest.json`.
- API coverage includes homepage delivery, health, sample ZIP import, search, graph, context export, investigations, HTML report preview, path traversal rejection, invalid ZIP/base64 rejection, non-GitHub URL rejection, and graph result bounds.

## Additional hardening applied

- Made the single-analysis reservation atomic under the process lock so two simultaneous requests cannot both pass the running-state check and race over the same clone directory.
- Avoids following a pre-existing symlink when preparing the clone destination.
- Launch scripts install only the web app dependencies by default. UnvibeCode CLI installation is optional, so ZIP-based workflows do not trigger external analysis or provider usage.

## Not verified / known boundaries

- A fresh live UnvibeCode review was not run in this environment. The prior attempt on a public repository stalled; the CLI end-to-end flow remains unverified.
- The bundled sample ZIP does not include generated HTML reports. The report endpoint was tested with a controlled fixture, not a real CLI-generated report set.
- Browser automation was not run here. Frontend JavaScript syntax and backend API behavior were checked, but this is not a claim of full cross-browser QA.
- This is a local single-user app. It binds to `127.0.0.1`; do not expose it publicly without authentication, per-user storage isolation, rate limiting, CSRF protections, and deployment hardening.
- The analysis endpoint is a long-running synchronous request. Large repositories or upstream CLI delays may exceed browser/proxy timeouts even though the server enforces a 15-minute review timeout.

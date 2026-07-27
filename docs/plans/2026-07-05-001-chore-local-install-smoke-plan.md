---
title: Local Install And Comedy Docs Smoke Test - Plan
type: chore
date: 2026-07-05
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

## Goal Capsule

Install Document Manager locally, make it reachable in a browser, and prove the running application can ingest and surface representative documents from the external comedy docs corpus supplied by the user.

Success means a fresh local runtime has all required app, OCR, and PDF dependencies; the app starts reliably; representative comedy docs are processed into the database; and a repeatable smoke command verifies health, auth setup, ingestion, document listing, and search behavior.

## Product Contract

### Requirements

- R1: The project must be installed with all runtime dependencies needed for the application, including OCR and PDF support.
- R2: The application must be started locally and left reachable at a concrete URL.
- R3: Basic document ingestion must work without requiring OpenAI or Azure credentials; AI metadata and vector features may be skipped when no provider key is configured.
- R4: Representative text-like documents from the user-provided comedy docs corpus must be processed through the same document pipeline the app uses for normal staged files.
- R5: Markdown files must be accepted as text-like documents so the comedy docs corpus can be tested directly, not only through its `.txt` mirrors.
- R6: Verification must exercise live app surfaces: health, initial user setup or login, document processing, document listing, and fallback text search.
- R7: Runtime artifacts such as SQLite databases, uploaded documents, Chroma data, generated logs, secrets, and sample copies must remain local ignored state, not committed source.
- R8: Any smoke-test administrator credentials must be scoped to the local development runtime and the app must bind only to localhost unless the operator explicitly changes it.

### Non-Goals

- NG1: Do not require paid or external AI credentials to complete the install smoke.
- NG2: Do not bulk-import the full comedy docs corpus; use a small representative sample to keep the smoke fast and repeatable.
- NG3: Do not redesign the frontend or document processing architecture.
- NG4: Do not publish runtime data or sample comedy documents into the repository.

### Acceptance Criteria

- AC1: A repeatable command can build/install the runtime and start the app.
- AC2: The running app returns healthy responses from its health endpoints.
- AC3: A smoke command creates or reuses a local admin account, stages representative `.md` and `.txt` comedy documents, waits for processing, and observes completed OCR/text extraction in the database.
- AC4: The app API can list the processed sample documents through an authenticated request.
- AC5: The app API can find at least one processed sample document through full-text search without semantic/vector search.
- AC6: The final working tree does not include generated databases, logs, staged files, stored documents, or credentials.

## Planning Contract

### Key Technical Decisions

- KTD1: Prefer the repository Docker image for local installation because host Python is 3.10 and host OCR/PDF tools are absent, while the Dockerfile already targets Python 3.12 and installs Tesseract plus Poppler.
- KTD2: Add Markdown support by treating `.md` and `.markdown` as text-like files in configuration, validation, MIME fallback, and OCR text extraction.
- KTD3: Add a repository smoke script that interacts with the live HTTP API and the local runtime data directory, so the install can be verified repeatedly without manual UI steps.
- KTD4: Keep the smoke corpus read-only and outside the repository; copy only temporary sample files into ignored runtime staging data.
- KTD5: Use fallback full-text search for verification because AI provider keys are optional and absent in a clean local install.

### Assumptions

- A1: Docker is available on the user machine and can build/run the project image.
- A2: The external comedy docs corpus named in the user request is available locally and can be read by the active user.
- A3: The smoke test can create a deterministic local admin user in a fresh database; if a database already exists, it can log in with the same credentials or report a clear setup mismatch.
- A4: Leaving the app running in a Docker container satisfies "up and running" for this project.

### Risks And Mitigations

- Risk: The app's database settings may preserve old `allowed_extensions` values from a prior runtime.
  Mitigation: The smoke should run against a fresh local runtime data directory, and Markdown support should be part of both default settings and extraction logic.
- Risk: Auth or CSRF behavior may block direct upload API use in a scripted smoke.
  Mitigation: Use initial setup and authenticated API calls for read/search verification, and stage files into the watched staging directory for processing.
- Risk: Docker build dependencies may shift or fail on the current machine.
  Mitigation: Capture the exact build/start failure if it occurs and fall back to a host virtualenv only if the missing host OCR/PDF layer can be installed cleanly.

## Implementation Units

### U1 - Enable Markdown Ingestion

Goal: Let the document pipeline treat Markdown as a first-class text-like document format.

Files:

- `app/config.py`
- `app/services/ocr_service.py`
- `app/services/document_processor.py`
- `app/utils/init_settings.py`
- `tests/test_ocr_service.py`

Approach:

- Extend default allowed extensions to include `md` and `markdown`.
- Reuse text-file extraction for `.md` and `.markdown`.
- Add MIME fallback mappings for Markdown and plain text.
- Add focused tests covering Markdown and text extraction behavior.

Test Scenarios:

- Extract text from a UTF-8 Markdown file.
- Extract text from a legacy plain text file.
- Reject an unsupported extension with the existing error path.

### U2 - Add Local Smoke Harness

Goal: Provide a repeatable command that proves a running local app can process the comedy docs sample set.

Files:

- `scripts/local_smoke_test.py`
- `README.md`

Approach:

- Add a script that accepts a base URL, runtime data directory, and external sample corpus path.
- Create or reuse the initial admin account through the public setup/login endpoints.
- Select a small mixed sample of Markdown and text files from the comedy docs corpus.
- Copy samples into the app staging directory with stable names.
- Poll the database until the expected documents have been processed.
- Verify authenticated document listing and full-text search through the API.
- Treat smoke credentials as local development credentials only, configurable through environment variables, and avoid printing secrets in normal output.
- Document the local install/start/smoke commands.

Test Scenarios:

- Run against a fresh local runtime and observe all selected sample docs processed.
- Re-run without committing runtime artifacts.
- Fail with actionable output if the external sample corpus path is unavailable.

### U3 - Install And Start Runtime

Goal: Build and run the application with all required dependencies installed.

Files:

- `Dockerfile`
- `docker-entrypoint.sh`
- `docker-entrypoint-aio.sh`

Approach:

- Build the Docker image from the repository.
- Start the app on an available local port with a stable container name and a bind-mounted ignored data directory.
- Bind the published port to `127.0.0.1` for the local smoke runtime.
- Confirm OCR and PDF tools are available inside the runtime image.
- Confirm the application health endpoint is reachable.

Test Scenarios:

- `docker build` completes successfully.
- The running container reports available `tesseract` and `pdftoppm`.
- `GET /api/health` returns a healthy response.

### U4 - End-To-End Comedy Docs Verification

Goal: Prove the installed app ingests and finds real sample documents from the comedy docs corpus.

Files:

- `scripts/local_smoke_test.py`
- `data/` ignored runtime state

Approach:

- Run the smoke harness against the started app and the external comedy docs corpus.
- Verify database rows include processed sample filenames and extracted text.
- Verify authenticated list and search endpoints return sample document results.
- Preserve concise command output for the final report.

Test Scenarios:

- At least one Markdown sample and one text sample are processed.
- At least one full-text query from the samples returns a document through `/api/search/`.
- No generated runtime artifacts appear as tracked files.

## Verification Contract

- V1: `pytest` passes for the focused OCR/text extraction tests.
- V2: Docker image build completes for the local install path.
- V3: Running container exposes the app at a concrete localhost URL.
- V4: Runtime dependency checks inside the container find Tesseract and Poppler.
- V5: Health checks pass against the live app.
- V6: The local smoke script passes against the external comedy docs corpus.
- V7: `git status --short --untracked-files=all` shows only intended source changes and no runtime data/secrets.

## Definition Of Done

- The project is installed locally through the selected runtime path.
- The app is running and reachable in the browser.
- Markdown and text comedy docs samples have been ingested successfully.
- Live health, auth, list, and search checks have passed.
- Any code/test/docs changes are reviewed and verified.
- Runtime artifacts remain ignored and out of source control.

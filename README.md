# AI Document Processing Platform

An AI-powered system that ingests business documents (invoices, purchase orders, contracts, resumes), automatically identifies the document type, extracts structured data, validates it deterministically, routes it through human review and correction, and exposes the approved result as structured JSON over a REST API.

## Architecture overview

```
Streamlit UI  ──HTTP──▶  FastAPI backend  ──▶  Gemini API (structured outputs)
(upload, review,          (pipeline + REST API)
 correct, approve)               │
                                 ▼
                          SQLite + uploads/
```

Every document flows through the same pipeline:

```
ingest (deterministic) → classify (AI) → extract (AI) → validate (deterministic)
      → human review & correction (UI) → approved structured JSON (API)
```

Three cleanly separated layers:

- **Backend (`backend/app/`)** — FastAPI service owning the pipeline, storage, and the REST contract. Uploads are processed asynchronously (background tasks); every state transition is persisted, so the API is always the source of truth.
- **Document-type registry (`backend/app/doctypes/`)** — each supported type is a self-contained spec (schema + prompts + validators). This is the system's single extension point.
- **Review UI (`ui/`)** — a Streamlit app that is a pure HTTP client of the backend. It holds no business logic; anything the UI can do, a downstream system can do through the same API.

Problem decomposition in one sentence: *the only sub-problems handed to AI are the two that are impossible with rules (classification, extraction); everything that can be decided by code — routing, validation, gating, storage — is code.*

## Setup

Requires Python 3.10+ and a Gemini API key ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)).

```bash
pip install -r requirements.txt
copy .env.example .env        # then put your GEMINI_API_KEY in .env

# terminal 1 — backend API
uvicorn backend.app.main:app --reload

# terminal 2 — review UI
streamlit run ui/streamlit_app.py
```

If port 8000 is taken on your machine, start the backend with `--port 8010` and set `API_BASE_URL=http://127.0.0.1:8010` in `.env` (the UI reads it from there).

Generate test documents (four PDFs, a DOCX resume, a random TXT — the invoice has a deliberately wrong total and the PDF resume a malformed email, so validation provably fires):

```bash
python samples/make_samples.py     # writes to samples/out/
```

Interactive API docs: http://127.0.0.1:8000/docs

## Live demo

### Hosted (Streamlit Community Cloud)

The app runs as a single container on Community Cloud: `ui/streamlit_app.py` has a small deployment shim that starts the FastAPI backend inside the container when no external backend is reachable (locally it's a no-op). To deploy your own instance:

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
2. **New app** → repository `Stuti711/Planbow_Assignment_AI`, branch `main`, main file `ui/streamlit_app.py`.
3. Under **Advanced settings → Secrets**, add: `GEMINI_API_KEY = "your-key"` (optionally `GEMINI_MODEL`).
4. Deploy. Ready-made test documents are in [`samples/out/`](samples/out/) — download and upload them in the app.

Notes for a public instance: there is no authentication (out of assignment scope), so all visitors see the same document queue, and they share the Gemini API quota of the configured key.

### Local walkthrough

Run locally with the four commands above (backend + UI), then in the UI:

1. **Upload** the files from `samples/out/` (PDF, DOCX and TXT).
2. **Documents** — watch each get classified with a confidence score; `random_notes.txt` comes back `unknown` and is flagged for manual typing.
3. **Review `invoice.pdf`** — validation flags the wrong total (`subtotal + tax ≠ total`); *Approve* is blocked until you correct the total, after which validation clears and approval succeeds. The AI's original stays stored next to your correction.
4. **Review `resume.pdf`** — the malformed email (`priya.sharma[at]example.com`) is extracted verbatim and flagged with a format warning.
5. `GET /documents/{id}/result` — the approved, corrected, structured JSON a downstream system would consume.

## REST API (the downstream integration surface)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/documents` | Upload (multipart); pipeline runs in the background |
| `GET` | `/documents` | List documents with status / type / confidence |
| `GET` | `/documents/{id}` | Full detail: extraction, corrections, confidences, validation issues |
| `PATCH` | `/documents/{id}/data` | Save human corrections (schema-checked, re-validated) |
| `POST` | `/documents/{id}/classify` | Manually set the type; re-runs extraction |
| `POST` | `/documents/{id}/approve` | Approve — blocked while error-severity validation issues remain |
| `POST` | `/documents/{id}/retry` | Re-run the pipeline after a failure |
| `GET` | `/documents/{id}/result` | **Final structured JSON for downstream systems** |
| `GET` | `/doctypes` | Supported document types (registry-driven) |

`GET /documents/{id}/result` returns the human-corrected data when corrections exist, otherwise the AI extraction, plus `approved`, confidences, validation state and file hash — everything a downstream ERP/ATS/DMS needs. Consumers should take only `approved: true` results.

## Design decisions

### 1. What is handled by AI?

Only the two problems that genuinely need it — both are "unstructured in, structured out" tasks that are impractical to solve with rules:

- **Classification**: one Gemini call returns `{doc_type, confidence, reasoning}` constrained to an enum generated from the type registry.
- **Extraction**: one Gemini call per document with the type's Pydantic schema enforced through structured outputs (`response_schema` → `response.parsed`), plus a self-reported confidence per field.

PDFs and images go to the model natively (multimodal input), which removes an entire OCR subsystem. Calls run at `temperature=0` with retry/backoff on transient errors.

### 2. What is handled by deterministic logic?

Everything whose correctness can be decided by code:

| Concern | Mechanism |
|---|---|
| File format detection | Extension + magic-byte checks (`ingest.py`) |
| DOCX/TXT text extraction | `python-docx` / plain read — no AI needed to read text |
| Schema enforcement | Pydantic validation of every model response — malformed output is rejected, never stored |
| Business-rule validation | Per-type validators: line items sum to subtotal, `subtotal + tax − discount = total`, date ordering, email/phone regex, required fields |
| Review routing | Confidence thresholds (`CONFIDENCE_THRESHOLD`) decide what gets flagged |
| Approval gate | Re-validation at approve time; error-severity issues block approval |
| Storage, API, status workflow | SQLite + FastAPI — no AI in the control path |

The dividing principle: **AI proposes, deterministic code disposes.** The model never gets to decide whether its own output is acceptable.

### 3. How is the system extensible for new document types?

Every document type is a self-contained `DocTypeSpec` in `backend/app/doctypes/` — a Pydantic schema, a description (used by the classification prompt), extraction hints, and a list of validator functions. Adding, say, *Bank Statement* support is:

1. Create `doctypes/bank_statement.py` defining a `SPEC`.
2. Add it to `REGISTRY` in `doctypes/registry.py`.

Nothing else changes: the classification enum, extraction schema, validation run, and the review form (which renders from the data itself) all derive from the registry.

### 4. How are incorrect or low-confidence AI responses handled?

Defense in depth, cheapest check first:

1. **Structured outputs** — the response is schema-constrained at the API level; malformed JSON can't enter the system.
2. **Classification threshold** — below `CONFIDENCE_THRESHOLD` (or `unknown`), the document is flagged and the reviewer picks the type manually; extraction re-runs.
3. **Per-field confidence** — the model reports confidence per field; low-confidence fields are visually flagged (⚠️) in the review UI.
4. **Deterministic validators** — catch *plausible but wrong* values the model is confident about (arithmetic that doesn't add up, impossible date ranges, malformed emails).
5. **Mandatory human review** — nothing is auto-approved. Corrections are stored separately (`corrected_data`) so the AI original stays available for comparison/audit, and approval is blocked while error-severity issues remain.
6. **Failure containment** — API errors mark the document `failed` with the message stored; a retry re-runs the pipeline.

### 5. How does the solution integrate with downstream systems?

The FastAPI service is itself the integration point: any downstream system (ERP, accounting, ATS) polls `GET /documents?` for `status=approved` and pulls `GET /documents/{id}/result` — a stable, typed JSON contract that includes the data, provenance (`was_corrected`, confidences, `sha256`), and validation state. Because the API is plain REST, adding push-style delivery later (webhooks, a message queue) is an additive change on top of the same result contract — deliberately out of scope here.

## AI models & tools used

| Layer | Choice | Why |
|---|---|---|
| AI model | **Google Gemini** — `gemini-3.1-flash-lite` by default, configurable via `GEMINI_MODEL` | Native multimodal input (PDFs/images without an OCR stage) and schema-enforced structured outputs. The lite tier has the most free-tier quota headroom; swap in `gemini-3.5-flash` or a pro-tier model for higher accuracy if your key has quota. |
| AI SDK | `google-genai` (official) | `response_schema` + `response.parsed` gives Pydantic-validated model output — malformed responses can't enter the system. All calls run at `temperature=0` with quota-aware retry/backoff (honors the server's "retry in Ns" hint) and a 3-call concurrency cap. |
| API framework | FastAPI | Typed request/response models, background tasks for async processing, generated OpenAPI docs at `/docs`. |
| Storage | SQLite via SQLModel | Zero-setup for reviewers; JSON columns for extraction payloads; swappable for Postgres by changing one connection string. |
| Review UI | Streamlit + httpx | Fast to build a functional human-in-the-loop UI; deliberately a thin HTTP client of the backend. |
| Schemas/validation | Pydantic v2 | One schema per document type drives extraction, correction validation, and the API contract. |
| Sample generation | reportlab / python-docx | Reproducible test corpus with deliberate defects so validation is demonstrable. |

## Assumptions made

- **One document per file.** A file contains a single logical document (no splitting of combined scans).
- **Documents are legible to a multimodal model.** PDFs may be digital or scanned; a dedicated OCR stage is unnecessary because Gemini reads pages natively. Illegible inputs surface as low-confidence fields rather than hard failures.
- **Every document is human-reviewed.** The assignment asks for review and correction, so there is no auto-approval path; AI reduces the manual work to *verification* rather than *transcription*.
- **Extraction must be verbatim.** The model is instructed to copy values exactly as printed (not to repair typos or malformed emails) so deterministic validation judges the document's real content. Verified in testing: the model initially "fixed" a malformed email until this rule was added.
- **Assignment-scale volume.** SQLite + in-process background tasks comfortably handle demo/assignment volume; the free-tier Gemini quota (per-model, per-minute/day) is the real throughput bound. See Future improvements for the production path.
- **Validation tolerance.** Monetary arithmetic checks use a ±0.02 tolerance for rounding; dates are accepted in common formats and warned on otherwise.
- **Trusted single operator.** No authentication/multi-tenancy — the platform runs as an internal tool for one reviewer (out of assignment scope).

## Future improvements

- **Scale-out processing** — replace in-process background tasks with a queue (e.g. Redis + worker pool), Postgres instead of SQLite, and batch API calls for cost at "hundreds of documents per day" volume.
- **Push integration** — webhooks/events on approval so downstream systems don't poll; the `/result` contract stays unchanged.
- **Confidence-gated auto-approval** — documents with high classification/field confidence and zero validation issues could skip review after an accuracy baseline is established, with sampling-based audit.
- **Evaluation harness** — a golden set of labeled documents with per-field accuracy metrics, so model/prompt changes are measured instead of eyeballed.
- **Provenance in review** — page/bounding-box citations for each extracted field so reviewers can verify against the exact source location instead of re-reading the document.
- **More document types** — receipts, bank statements, delivery notes: each is one registry entry.
- **Hardening** — authentication and roles, per-tenant isolation, document retention policies, structured logging/tracing and per-document AI cost tracking, Docker Compose for one-command startup.

## Project structure

```
backend/app/
├── main.py          # FastAPI routes
├── config.py        # .env settings (model, threshold, paths)
├── db.py, models.py # SQLite via SQLModel; single documents table
├── ingest.py        # format detection, DOCX/TXT text extraction   [deterministic]
├── pipeline.py      # classify → extract → validate orchestration
├── ai/
│   ├── client.py    # Gemini wrapper: structured outputs, temp=0, retries
│   ├── classifier.py# type classification (registry-driven enum)   [AI]
│   └── extractor.py # per-type schema extraction + field confidence [AI]
└── doctypes/
    ├── base.py      # DocTypeSpec + shared validator helpers
    ├── registry.py  # THE extension point
    └── invoice.py, purchase_order.py, contract.py, resume.py
ui/streamlit_app.py  # upload / documents / review pages (HTTP client only)
samples/make_samples.py
```

## Scope notes

Deliberately excluded as out of assignment scope: authentication, webhooks/queues, auto-approval, multi-tenancy. The status workflow is `uploaded → processing → needs_review → approved` (`failed` on error) — every document passes through human review.

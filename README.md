# AI Document Processing Platform

**By Stuti Nagaich**

I built this to solve a concrete business problem: organizations receive hundreds of documents a day — invoices, purchase orders, contracts, resumes — and processing them by hand doesn't scale. My goal was to design a system where AI does the part it's actually good at (reading unstructured documents) while everything that needs to be *correct* — validation, gating, storage, the API contract — stays deterministic and auditable.

The result is a platform that accepts a document, automatically figures out what kind of document it is, pulls out the structured fields, checks the extracted data against business rules, lets a human review and fix anything wrong, and exposes the final result as clean JSON for any downstream system to consume.

## Architecture overview

```
Streamlit UI  ──HTTP──▶  FastAPI backend  ──▶  Gemini API (structured outputs)
(upload, review,          (pipeline + REST API)
 correct, approve)               │
                                 ▼
                          SQLite + uploads/
```

I designed every document to move through the same fixed pipeline:

```
ingest (deterministic) → classify (AI) → extract (AI) → validate (deterministic)
      → human review & correction (UI) → approved structured JSON (API)
```

I split the system into three layers with a clean boundary between them:

- **Backend (`backend/app/`)** — the FastAPI service that owns the pipeline, the storage, and the REST contract. Uploads process asynchronously in the background, and I persist every state transition so the API is always the single source of truth.
- **Document-type registry (`backend/app/doctypes/`)** — each document type I support is a self-contained spec: a schema, extraction prompts, and validators. This is the one place I built the system to be extended from.
- **Review UI (`ui/`)** — a Streamlit app that only ever talks to the backend over plain HTTP. I kept it deliberately dumb: it holds no business logic, so anything the UI can do, any other client can do through the same API.

The way I decomposed the problem in one sentence: *hand AI only the two sub-problems that are genuinely impossible to solve with rules — classification and extraction — and keep everything else (routing, validation, approval, storage) as ordinary, testable code.*

## Setup

You'll need Python 3.10+ and a Gemini API key ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)).

```bash
pip install -r requirements.txt
copy .env.example .env        # then put your GEMINI_API_KEY in .env

# terminal 1 — backend API
uvicorn backend.app.main:app --reload

# terminal 2 — review UI
streamlit run ui/streamlit_app.py
```

If port 8000 is already taken, I run the backend on `--port 8010` and set `API_BASE_URL=http://127.0.0.1:8010` in `.env` — the UI reads the backend location from there.

I also wrote a sample generator so there's always test data on hand — four PDFs, a DOCX resume, and a random TXT, with a deliberately wrong invoice total and a malformed email baked in so the validation layer has something real to catch:

```bash
python samples/make_samples.py     # writes to samples/out/
```

Interactive API docs: http://127.0.0.1:8000/docs

## Live demo

**Live app:** https://document-ai-planbow.streamlit.app/

### Hosted (Streamlit Community Cloud)

I deployed this as a single container on Community Cloud — `ui/streamlit_app.py` includes a small bootstrap I wrote that starts the FastAPI backend inside the same container when no external backend is reachable (locally this is a no-op, since I run the backend as its own process there). To spin up your own copy:

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
2. **New app** → repository `Stuti711/Planbow_Assignment_AI`, branch `main`, main file `ui/streamlit_app.py`.
3. Under **Advanced settings → Secrets**, add: `GEMINI_API_KEY = "your-key"` (optionally `GEMINI_MODEL`).
4. Deploy. I've committed ready-made test documents in [`samples/out/`](samples/out/) — download and upload them straight into the app.

Since there's no authentication (a deliberate scope decision, see below), every visitor to a hosted instance shares the same document queue and the same Gemini quota.

### Local walkthrough

Run it locally with the four commands above, then in the UI:

1. **Upload** the files from `samples/out/` (PDF, DOCX and TXT).
2. **Documents** — watch each one get classified with a confidence score; `random_notes.txt` comes back `unknown`, which is exactly what I wanted it to do since it isn't a business document.
3. **Review `invoice.pdf`** — validation catches the wrong total (`subtotal + tax ≠ total`) I baked into the sample. *Approve* is blocked until the total is corrected; once fixed, validation clears and approval succeeds. I keep the AI's original extraction stored next to the correction for audit.
4. **Review `resume.pdf`** — the malformed email (`priya.sharma[at]example.com`) comes through verbatim and gets flagged with a format warning, rather than being silently "fixed" by the model.
5. `GET /documents/{id}/result` — the approved, corrected, structured JSON that a downstream system would actually consume.

## REST API (the downstream integration surface)

I exposed the pipeline entirely through REST, so any system that can make an HTTP call can integrate:

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

`GET /documents/{id}/result` returns the human-corrected data when it exists, otherwise the raw AI extraction, plus `approved`, per-field confidences, validation state, and a file hash — everything I'd want if I were wiring this into an ERP, ATS, or document management system. I designed downstream consumers to only trust `approved: true` results.

## Design decisions

These are the questions I thought hardest about while building this — more than the app itself, they're the actual engineering behind it.

### 1. What did I hand to AI?

Only the two problems that genuinely need it — both are "unstructured in, structured out" tasks that are impractical to solve with rules:

- **Classification** — one Gemini call returns `{doc_type, confidence, reasoning}`, constrained to an enum I generate from the type registry.
- **Extraction** — one Gemini call per document, with the type's Pydantic schema enforced through structured outputs (`response_schema` → `response.parsed`), plus a self-reported confidence per field.

I send PDFs and images to the model natively as multimodal input, which meant I never had to build an OCR subsystem. Every call runs at `temperature=0` with retry/backoff on transient errors.

### 2. What did I keep deterministic?

Everything whose correctness I could decide with code, I did:

| Concern | Mechanism |
|---|---|
| File format detection | Extension + magic-byte checks (`ingest.py`) |
| DOCX/TXT text extraction | `python-docx` / plain read — no AI needed to read plain text |
| Schema enforcement | Pydantic validation on every model response — malformed output is rejected, never stored |
| Business-rule validation | Per-type validators: line items sum to subtotal, `subtotal + tax − discount = total`, date ordering, email/phone regex, required fields |
| Review routing | Confidence thresholds (`CONFIDENCE_THRESHOLD`) decide what gets flagged for a human |
| Approval gate | Re-validation at approve time; error-severity issues block approval |
| Storage, API, status workflow | SQLite + FastAPI — no AI anywhere in the control path |

My guiding principle: **AI proposes, deterministic code disposes.** The model never gets to decide whether its own output is good enough — my code does.

### 3. How did I make it extensible to new document types?

I built every document type as a self-contained `DocTypeSpec` in `backend/app/doctypes/` — a Pydantic schema, a description the classifier uses, extraction hints, and a list of validators. So if I wanted to add, say, *Bank Statement* support, it's:

1. Create `doctypes/bank_statement.py` defining a `SPEC`.
2. Add it to `REGISTRY` in `doctypes/registry.py`.

Nothing else needs to change — the classification enum, the extraction schema, the validation run, and the review form (which renders straight from the data) all derive from that registry.

### 4. How did I handle incorrect or low-confidence AI output?

I layered defenses, cheapest check first:

1. **Structured outputs** — the response is schema-constrained at the API level, so malformed JSON can't enter the system at all.
2. **Classification threshold** — below `CONFIDENCE_THRESHOLD` (or `unknown`), I flag the document and let the reviewer pick the type manually, which re-triggers extraction.
3. **Per-field confidence** — the model reports a confidence per field, and I surface low-confidence fields with a ⚠️ in the review UI.
4. **Deterministic validators** — these catch *plausible but wrong* values the model was confident about (arithmetic that doesn't add up, impossible date ranges, malformed emails).
5. **Mandatory human review** — nothing auto-approves. I store corrections separately (`corrected_data`) so the AI's original stays available for comparison, and I block approval while error-severity issues remain.
6. **Failure containment** — if a call errors out, I mark the document `failed` with the reason stored, and a retry just re-runs the pipeline.

### 5. How does this integrate with downstream systems?

I made the FastAPI service itself the integration point — any downstream system (ERP, accounting, ATS) polls `GET /documents` for `status=approved` and pulls `GET /documents/{id}/result`: a stable, typed JSON contract carrying the data, provenance (`was_corrected`, confidences, `sha256`), and validation state. Because I kept the API plain REST, adding push-style delivery later (webhooks, a message queue) is purely additive on top of the same result contract — I left that out deliberately for now.

## AI models & tools used

| Layer | What I used | Why I chose it |
|---|---|---|
| AI model | **Google Gemini** — `gemini-3.1-flash-lite` by default, configurable via `GEMINI_MODEL` | Native multimodal input meant I could feed it PDFs and images directly, with no OCR stage, and its schema-enforced structured outputs give me guaranteed-valid JSON. The lite tier has the most free-tier quota headroom for a project like this; I can swap in `gemini-3.5-flash` or a pro-tier model for higher accuracy if quota allows. |
| AI SDK | `google-genai` (official) | `response_schema` + `response.parsed` gives me Pydantic-validated model output directly — malformed responses can't get past this layer. I run every call at `temperature=0` with quota-aware retry/backoff (it honors the server's "retry in Ns" hint) and cap concurrency at 3 in-flight calls. |
| API framework | FastAPI | Typed request/response models, background tasks for async processing, and free OpenAPI docs at `/docs`. |
| Storage | SQLite via SQLModel | Zero setup for anyone reviewing this; JSON columns hold the extraction payloads; swapping to Postgres later is a one-line connection-string change. |
| Review UI | Streamlit + httpx | Let me build a working human-in-the-loop UI fast, while keeping it a thin HTTP client of the backend by design. |
| Schemas/validation | Pydantic v2 | One schema per document type drives extraction, correction validation, and the API contract — I only had to define each type once. |
| Sample generation | reportlab / python-docx | I wrote this so I'd have a reproducible test corpus with deliberate defects, to prove the validation layer actually works rather than just trusting it. |

## Assumptions made

- **One document per file.** I assumed a file holds a single logical document — no splitting combined scans.
- **Documents are legible to a multimodal model.** PDFs can be digital or scanned; I didn't build a dedicated OCR stage because Gemini reads pages natively. Illegible input shows up as low-confidence fields rather than a hard failure.
- **Every document gets human-reviewed.** The brief asks for review and correction, so I didn't build an auto-approval path — AI's job here is to reduce the work to *verification*, not replace it with *transcription*.
- **Extraction has to be verbatim.** I instruct the model to copy values exactly as printed rather than repair typos or malformed emails, so my validators are judging the document's real content. I actually caught this the hard way in testing: the model was silently "fixing" a malformed email until I added this rule.
- **This is built for assignment-scale volume.** SQLite plus in-process background tasks handle demo volume comfortably; the real throughput ceiling right now is the free-tier Gemini quota. I've noted the production path below.
- **Validation needs tolerance.** I use a ±0.02 tolerance on monetary arithmetic for rounding, and accept dates in a handful of common formats before warning.
- **Trusted single operator.** There's no authentication or multi-tenancy — I built this as an internal tool for one reviewer, which is out of scope for the assignment.

## Future improvements

If I took this further, here's where I'd invest next:

- **Scale-out processing** — swap the in-process background tasks for a real queue (Redis + workers), move off SQLite to Postgres, and batch API calls for cost at hundreds-of-documents-a-day volume.
- **Push integration** — emit webhooks on approval instead of making downstream systems poll; the `/result` contract wouldn't need to change.
- **Confidence-gated auto-approval** — once I have an accuracy baseline, documents with high confidence and zero validation issues could skip review, with sampling-based audits to keep it honest.
- **An evaluation harness** — a golden set of labeled documents with per-field accuracy metrics, so I can measure prompt/model changes instead of eyeballing them.
- **Provenance in review** — page or bounding-box citations per extracted field, so a reviewer can jump straight to the source instead of re-reading the whole document.
- **More document types** — receipts, bank statements, delivery notes — each is just one more registry entry.
- **Hardening** — auth and roles, per-tenant isolation, retention policies, structured logging/tracing, per-document AI cost tracking, and a Docker Compose setup for one-command startup.

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
    ├── registry.py  # the extension point
    └── invoice.py, purchase_order.py, contract.py, resume.py
ui/streamlit_app.py  # upload / documents / review pages (HTTP client only)
samples/make_samples.py
```

## Scope notes

I deliberately left a few things out because they weren't part of the brief: authentication, webhooks/queues, auto-approval, multi-tenancy. The status workflow I built is `uploaded → processing → needs_review → approved` (`failed` on error) — every document passes through human review before it counts as done.

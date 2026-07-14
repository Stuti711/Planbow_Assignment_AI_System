# AI Document Processing Platform

**Built by Stuti Nagaich**

**Live app:** https://planbow-document-ai.streamlit.app/ 

---

## The problem I set out to solve

Organizations receive hundreds of business documents a day — invoices, purchase orders, contracts, resumes — and handling them by hand is slow, error-prone, and impossible to scale. I wanted to build a system that removes the manual grind without ever pretending the machine is infallible.

The principle I started from, and kept returning to at every decision point: **let AI do the one thing it's genuinely good at — reading messy, unstructured documents — and keep everything that has to be *correct* in plain, testable, deterministic code.** An AI model is excellent at looking at a PDF and telling you "this is an invoice, and here are its fields." It is not something I'd trust to silently decide whether its own answer is right. So I drew a hard line: the model *proposes*, my code *disposes*.

Everything below follows from that one decision.

## What the system does

A document enters and moves through a fixed, auditable pipeline. I built each stage to have a single, clear responsibility:

```
ingest → classify → extract → validate → human review → approved structured output
(rules)   (AI)       (AI)      (rules)     (person)       (JSON via API)
```

1. **Ingest** — the file is accepted, its real format verified (not just trusted by extension), and stored. PDFs and images go on untouched; Word and text files have their text pulled out here.
2. **Classify** — the model identifies what *kind* of document it is, and reports how confident it is.
3. **Extract** — the model pulls out the fields that matter *for that specific type* — an invoice's line items and totals, a resume's work history — into a strict, validated structure.
4. **Validate** — my own rules check the extracted data: do the invoice numbers add up, is the email well-formed, do the dates make sense.
5. **Review** — a person sees the result, with anything low-confidence or failing validation flagged, and can correct it. Nothing is auto-approved.
6. **Deliver** — once approved, the clean structured data is available as JSON over a REST API for any downstream system to pull.

## Requirements coverage

Everything the brief asked for, and where it lives in the code:

| Requirement | How the solution meets it |
|---|---|
| Accept multiple document types | Accepts PDF, PNG/JPG, DOCX, and TXT (`backend/app/ingest.py`) |
| Automatically identify the document type | AI classification with a confidence score; returns `unknown` when unsure rather than guessing (`ai/classifier.py`) |
| Extract relevant structured information | Type-specific, schema-enforced extraction — each document type has its own fields (`ai/extractor.py`, `doctypes/`) |
| Validate the extracted information | Deterministic business-rule validators — arithmetic, date order, email/phone formats, required fields (`doctypes/*.py`) |
| Allow users to review and correct | Streamlit review screen with low-confidence flags, inline editing, and an approval gate (`ui/streamlit_app.py`) |
| Expose the processed data in a structured format | REST API returning JSON — `GET /documents/{id}/result` |

## Architecture overview

```
Streamlit UI  ──HTTP──▶  FastAPI backend  ──▶  Gemini API (structured outputs)
(upload, review,          (pipeline + REST API)
 correct, approve)               │
                                 ▼
                          SQLite + file storage
```

I built it as three layers with a deliberately clean boundary between them:

- **Backend (`backend/app/`)** — the FastAPI service that owns the processing pipeline, the database, and the REST contract. Uploads are processed in the background so the UI never blocks, and every state change is persisted, which makes the API the single source of truth for a document's status.
- **Document-type registry (`backend/app/doctypes/`)** — every document type is a self-contained definition: its data schema, the hints the classifier and extractor use, and its validation rules. This is the one place the system is designed to grow from.
- **Review UI (`ui/`)** — a Streamlit app that talks to the backend only over HTTP. I kept it deliberately thin — it holds no business logic — so that anything a human can do through the screen, another system can do through the same API.

## Design decisions

This is the part of the project I care about most. The brief asked me to *think* about five questions; here's how I answered each in the actual build.

### What I handed to AI, and what I kept deterministic

I gave the model exactly two jobs, because both are "unstructured in, structured out" problems that rules can't solve:

- **Classification** — one call returns the document type, a confidence score, and a one-line reason. The list of possible types is generated from my registry, so the model can only ever answer with a type the system actually supports (or `unknown`).
- **Extraction** — one call per document, where I force the output to match that type's schema exactly, and ask the model to self-report a confidence for each field.

Everything else is ordinary code, because everything else can be *checked*:

| Handled by deterministic code | How |
|---|---|
| File-format detection | Extension **and** magic-byte checks, so a renamed file can't slip through |
| Reading Word/text files | `python-docx` and plain reads — no AI needed to read text that's already text |
| Guaranteeing valid output | Every model response is validated against its schema; malformed output is rejected, never stored |
| Business rules | Line items sum to the subtotal, `subtotal + tax − discount = total`, dates are ordered correctly, emails and phones are well-formed, required fields are present |
| Deciding what needs a human | A confidence threshold routes uncertain results to review |
| The approval gate | Data is re-validated at approval; anything with an error can't be approved |
| Storage, status, the API | SQLite + FastAPI — no AI anywhere in the control path |

The dividing line in one sentence: **AI proposes, deterministic code disposes.** The model never gets to rubber-stamp its own work.

### How I made it extensible for new document types

Adding a new type — say, a bank statement — is two steps, and touches nothing else:

1. Write one file in `backend/app/doctypes/` defining its schema, hints, and validators.
2. Register it in `registry.py`.

The classification options, the extraction schema, the validation run, and even the review form (which builds itself from the data) all read from that registry. I built it this way on purpose so the system scales by *addition*, not by editing the pipeline. The four types shipped — invoice, purchase order, contract, resume — are each just one such file, which is the proof the pattern holds.

### How I handled incorrect or low-confidence AI responses

I don't trust a single check, so I layered them, cheapest first:

1. **Schema enforcement** — malformed output can't enter the system at all.
2. **Classification confidence** — if the model isn't sure what the document is, it's flagged and a person picks the type, which re-runs extraction against the right schema.
3. **Per-field confidence** — uncertain fields are visibly flagged in the review screen so a human knows exactly where to look.
4. **Validation rules** — these catch the dangerous case: values the model was *confident* about but that are still wrong (totals that don't add up, impossible dates, malformed emails).
5. **Mandatory human review** — nothing is approved automatically. Corrections are stored *separately* from the AI's original output, so the original is always available for comparison and audit.
6. **Failure containment** — if a call fails, the document is marked failed with the reason recorded, and can be retried without re-uploading.

> A real example from building this: I found the model was silently "correcting" a malformed email (`x[at]y.com` → `x@y.com`) during extraction — which would have hidden the very error my validation was meant to catch. I fixed it by instructing the model to copy values *verbatim* and let the deterministic layer judge them. That's the whole philosophy in miniature.

### How it integrates with downstream systems

The FastAPI service *is* the integration point. A downstream system (an ERP, an accounting tool, an applicant-tracking system) lists documents, filters for approved ones, and pulls the final result — a stable, typed JSON payload that carries the data plus its provenance: whether it was human-corrected, the confidence scores, the validation state, and a hash of the source file. Because it's plain REST, adding push-style delivery later (webhooks, a queue) is purely additive and doesn't change that contract.

## AI models & tools used

| Layer | Choice | Why |
|---|---|---|
| AI model | **Google Gemini** (`gemini-3.1-flash-lite` by default, configurable) | It reads PDFs and images *natively*, which let me skip building an OCR stage entirely, and its structured-output mode guarantees the response matches my schema. The lite tier gives the most headroom on a free key; a higher tier can be swapped in for more accuracy. |
| AI SDK | `google-genai` (official) | Returns model output already validated against a Pydantic schema, so bad output can't get past that boundary. I run every call deterministically (`temperature=0`) with retry/backoff that respects the API's own rate-limit hints, and cap concurrent calls so a burst of uploads doesn't trip quotas. |
| Backend / API | **FastAPI** | Typed requests and responses, background processing, and automatic interactive API docs. |
| Storage | **SQLite** (via SQLModel) | Runs with zero setup so anyone can clone and start; moving to Postgres for production is a one-line change. |
| Review UI | **Streamlit** | Let me build a genuinely usable review-and-correct interface quickly, while keeping it a thin client of the backend. |
| Schemas & validation | **Pydantic** | One schema per document type drives extraction, correction-checking, and the API contract from a single definition. |
| Test data | **reportlab / python-docx** | I generate a sample set with *deliberate* defects (a wrong invoice total, a malformed email) so the validation layer can be demonstrated, not just claimed. |

## Assumptions made

- **One document per file** — I assume a file is a single logical document, not a stack of combined scans.
- **Documents are legible to a multimodal model** — I skipped a dedicated OCR stage because Gemini reads pages directly; a genuinely illegible scan surfaces as low-confidence fields rather than a crash.
- **Every document is reviewed by a person** — the brief asked for review and correction, so I built no auto-approval path. AI reduces the work to *verifying*, not blind *transcribing*.
- **Extraction is verbatim** — the model copies what's printed rather than "fixing" it, so validation judges the document's real content.
- **Built for assignment-scale volume** — SQLite and background tasks handle this comfortably; the real ceiling right now is the free-tier AI quota, and I've mapped the production path below.
- **Sensible tolerances** — money checks allow a small rounding tolerance; dates are accepted in common formats and only warned on otherwise.
- **A single trusted operator** — no authentication or multi-tenancy, since that was out of scope for this brief.

## Future improvements

Where I'd invest next if this went to production:

- **Scale-out processing** — replace in-process background tasks with a real job queue and workers, move to Postgres, and batch API calls for cost at hundreds-of-documents-a-day volume.
- **Push integration** — emit webhooks when a document is approved, so downstream systems don't have to poll; the result contract stays the same.
- **Confidence-gated auto-approval** — once there's an accuracy baseline, let high-confidence, clean documents skip review, backed by sampling audits.
- **An evaluation harness** — a labeled golden set with per-field accuracy metrics, so prompt and model changes are measured, not eyeballed.
- **Field-level provenance** — page/coordinate citations per field so a reviewer jumps straight to the source instead of re-reading the document.
- **More document types** — receipts, delivery notes, bank statements — each is one more registry entry.
- **Production hardening** — authentication and roles, per-tenant isolation, retention policies, logging/tracing, per-document cost tracking, and a one-command Docker setup.

## Live demo

**Try it:** https://planbow-document-ai.streamlit.app/

Test documents are included in [`samples/out/`](samples/out/) — download them and upload them in the app. A good five-minute walkthrough:

1. **Upload** the sample files (mix of PDF, DOCX, TXT).
2. On **Documents**, watch each one get classified with a confidence score — and note that a page of random notes correctly comes back as `unknown` rather than being forced into a category.
3. **Open the invoice** — validation catches the total that doesn't add up. *Approve* is blocked until it's corrected; fix it, and approval goes through. The AI's original value stays on record next to your correction.
4. **Open the resume** — the malformed email is flagged rather than silently fixed.
5. The **Structured output** panel shows the exact JSON a downstream system would receive.

> **On the API in this hosted demo:** the free hosting tier exposes only the UI publicly; the backend runs alongside it inside the same container. So the live link demonstrates the full product end-to-end and shows the real API's JSON output on screen, but to call the REST endpoints directly (with interactive docs at `/docs`) you run it locally — same API, just an externally reachable port. Pointing the UI at a separately hosted backend is a one-line config change.

## Running it locally

Requires Python 3.10+ and a Gemini API key ([get one here](https://aistudio.google.com/apikey)).

```bash
pip install -r requirements.txt
cp .env.example .env          # add your GEMINI_API_KEY

uvicorn backend.app.main:app  # the backend API + pipeline
streamlit run ui/streamlit_app.py   # the review UI (separate terminal)
```

Generate the sample documents to test with:

```bash
python samples/make_samples.py
```

Interactive API docs are then at `/docs` on the backend. Configuration (model, confidence threshold, backend URL) lives in `.env`.

## REST API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/documents` | Upload a document; processing runs in the background |
| `GET` | `/documents` | List documents with status, type, and confidence |
| `GET` | `/documents/{id}` | Full detail: extraction, corrections, confidences, validation issues |
| `PATCH` | `/documents/{id}/data` | Save human corrections (schema-checked, re-validated) |
| `POST` | `/documents/{id}/classify` | Set the type manually; re-runs extraction |
| `POST` | `/documents/{id}/approve` | Approve — blocked while validation errors remain |
| `POST` | `/documents/{id}/retry` | Re-run the pipeline after a failure |
| `GET` | `/documents/{id}/result` | **The final structured JSON for downstream systems** |
| `GET` | `/doctypes` | Supported document types |

## Project structure

```
backend/app/
├── main.py          # REST API routes
├── config.py        # settings (model, threshold, paths)
├── db.py, models.py # database — one documents table
├── ingest.py        # format detection + text extraction   [deterministic]
├── pipeline.py      # classify → extract → validate orchestration
├── ai/
│   ├── client.py    # Gemini wrapper: structured outputs, retries
│   ├── classifier.py# document-type classification           [AI]
│   └── extractor.py # per-type field extraction              [AI]
└── doctypes/
    ├── base.py      # the type-definition contract + shared validators
    ├── registry.py  # the extension point
    └── invoice.py, purchase_order.py, contract.py, resume.py
ui/streamlit_app.py  # the review interface (HTTP client only)
samples/make_samples.py   # generates test documents
```

Every document runs the workflow `uploaded → processing → needs_review → approved` (or `failed`, recoverably), and always passes a human before it counts as done.

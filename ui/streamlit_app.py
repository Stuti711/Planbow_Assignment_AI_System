"""Streamlit review UI — talks to the FastAPI backend over HTTP only.

Pages: Upload (submit documents), Documents (processing queue),
Review (inspect, correct, re-classify, approve).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.75"))

st.set_page_config(page_title="Document Processing Platform", layout="wide")


@st.cache_resource
def ensure_backend() -> None:
    """Start the backend if it isn't already reachable.

    Local dev runs uvicorn separately, so this is a no-op. On a single-container
    host (Streamlit Cloud) there's no second process, so start one here and
    bridge the deployment secrets into its environment first.
    """
    try:
        httpx.get(f"{API_BASE}/doctypes", timeout=2)
        return  # backend already running (normal local setup)
    except httpx.HTTPError:
        pass

    try:  # st.secrets raises if no secrets.toml exists at all (normal locally)
        for key in ("GEMINI_API_KEY", "GEMINI_MODEL", "CONFIDENCE_THRESHOLD"):
            if key in st.secrets:
                os.environ[key] = str(st.secrets[key])
    except Exception as exc:
        if not os.getenv("GEMINI_API_KEY"):
            st.warning(f"Could not read Streamlit secrets: {exc}")

    if not os.getenv("GEMINI_API_KEY"):
        st.error(
            "GEMINI_API_KEY is not configured for this deployment. Open this "
            'app\'s **⋮ menu → Settings → Secrets**, add `GEMINI_API_KEY = '
            '"your-key"`, save, then use **Reboot app** (a page refresh is '
            "not enough — the backend process must restart to pick up the key)."
        )
        st.stop()

    port = API_BASE.rsplit(":", 1)[-1].strip("/") or "8000"
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app.main:app",
         "--host", "127.0.0.1", "--port", port],
        cwd=ROOT,
    )
    for _ in range(30):
        try:
            httpx.get(f"{API_BASE}/doctypes", timeout=2)
            return
        except httpx.HTTPError:
            time.sleep(1)
    st.error("Backend failed to start — check the app logs.")
    st.stop()


ensure_backend()

STATUS_BADGES = {
    "processing": "🔄 processing",
    "needs_review": "📝 needs review",
    "approved": "✅ approved",
    "failed": "❌ failed",
    "uploaded": "⬆️ uploaded",
}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api(method: str, path: str, **kwargs):
    try:
        resp = httpx.request(method, f"{API_BASE}{path}", timeout=120, **kwargs)
    except httpx.ConnectError:
        st.error(f"Cannot reach the backend at {API_BASE}. "
                 "Start it with: uvicorn backend.app.main:app")
        st.stop()
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        return None, detail
    return resp.json(), None


# ---------------------------------------------------------------------------
# Dynamic review form (driven by the extracted data itself)
# ---------------------------------------------------------------------------

def _label(key: str, confidences: dict) -> str:
    label = key.replace("_", " ").title()
    conf = confidences.get(key)
    if conf is not None and conf < CONFIDENCE_THRESHOLD:
        label += f"  ⚠️ low confidence ({conf:.0%})"
    return label


def _records_from_editor(df: pd.DataFrame) -> list[dict]:
    # JSON round-trip converts NaN -> null and numpy scalars -> plain types.
    return json.loads(df.to_json(orient="records"))


def render_form(doc: dict) -> dict:
    """Render editable widgets for every field; return the edited data dict."""
    data = doc.get("corrected_data") or doc.get("extracted_data") or {}
    confidences = doc.get("field_confidences") or {}
    doc_id = doc["id"]
    edited: dict = {}

    scalar_keys = [k for k, v in data.items() if not isinstance(v, (list, dict))]
    complex_keys = [k for k in data if k not in scalar_keys]

    cols = st.columns(2)
    for i, key in enumerate(scalar_keys):
        value = data[key]
        widget_key = f"fld_{doc_id}_{key}"
        with cols[i % 2]:
            if isinstance(value, bool):
                edited[key] = st.checkbox(_label(key, confidences), value, key=widget_key)
            else:
                text = st.text_input(
                    _label(key, confidences),
                    "" if value is None else str(value),
                    key=widget_key,
                )
                edited[key] = text.strip() if text.strip() != "" else None

    for key in complex_keys:
        value = data[key]
        st.markdown(f"**{_label(key, confidences)}**")
        widget_key = f"fld_{doc_id}_{key}"
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            edited_df = st.data_editor(
                pd.DataFrame(value), num_rows="dynamic",
                use_container_width=True, key=widget_key,
            )
            edited[key] = _records_from_editor(edited_df)
        elif isinstance(value, list):
            text = st.text_area(
                "One entry per line", "\n".join(str(x) for x in value),
                key=widget_key, label_visibility="collapsed",
            )
            edited[key] = [line.strip() for line in text.splitlines() if line.strip()]
        else:  # rare fallback: raw JSON editing
            text = st.text_area("JSON", json.dumps(value, indent=2), key=widget_key)
            try:
                edited[key] = json.loads(text)
            except json.JSONDecodeError:
                st.warning(f"'{key}' is not valid JSON; keeping the previous value.")
                edited[key] = value
    return edited


def show_issues(issues: list) -> None:
    if not issues:
        st.success("All deterministic validation checks passed.")
        return
    for issue in issues:
        line = f"**{issue['field']}** · {issue['rule']} — {issue['message']}"
        if issue["severity"] == "error":
            st.error(line)
        else:
            st.warning(line)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def page_upload():
    st.header("Upload documents")
    doctypes, _ = api("GET", "/doctypes")
    types_str = (", ".join(t["display_name"] for t in doctypes)
                 if doctypes else "Invoice, Purchase Order, Contract, Resume")
    st.caption(f"Supported document types: {types_str}. "
               "File formats: PDF, PNG, JPG, DOCX, TXT. Each document is classified, "
               "extracted and validated automatically, then queued for review.")
    files = st.file_uploader(
        "Choose files", type=["pdf", "png", "jpg", "jpeg", "docx", "txt"],
        accept_multiple_files=True,
    )
    if files and st.button("Process documents", type="primary"):
        for f in files:
            result, err = api(
                "POST", "/documents",
                files={"file": (f.name, f.getvalue(), f.type or "application/octet-stream")},
            )
            if err:
                st.error(f"{f.name}: {err}")
            else:
                st.success(f"{f.name}: accepted (document #{result['id']}), processing started.")
        st.info("Track progress on the **Documents** page.")


def page_documents():
    st.header("Documents")
    docs, err = api("GET", "/documents")
    if err:
        st.error(err)
        return
    if not docs:
        st.info("No documents yet — upload some on the **Upload** page.")
        return

    rows = [{
        "ID": d["id"],
        "File": d["filename"],
        "Type": d["doc_type"] or "—",
        "Status": d["status"].replace("_", " "),
        "Classification confidence": (
            f"{d['classification_confidence']:.0%}"
            if d["classification_confidence"] is not None else "—"
        ),
        "Validation errors": "yes" if d["has_validation_errors"] else "no",
        "Uploaded": d["uploaded_at"][:19].replace("T", " "),
    } for d in docs]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    options = {f"#{d['id']} — {d['filename']} ({d['status']})": d["id"] for d in docs}
    choice = st.selectbox("Select a document", list(options))
    if st.button("Open review", type="primary"):
        st.session_state["doc_id"] = options[choice]
        st.session_state["page"] = "Review"
        st.rerun()

    if any(d["status"] == "processing" for d in docs):
        st.caption("Some documents are still processing — refresh to update.")
        if st.button("Refresh"):
            st.rerun()


def page_review():
    doc_id = st.session_state.get("doc_id")
    if doc_id is None:
        st.info("Pick a document on the **Documents** page first.")
        return

    doc, err = api("GET", f"/documents/{doc_id}")
    if err:
        st.error(err)
        return

    st.header(f"Review · #{doc['id']} {doc['filename']}")
    badge = STATUS_BADGES.get(doc["status"], doc["status"])
    conf = doc["classification_confidence"]
    st.markdown(
        f"**Status:** {badge} &nbsp;|&nbsp; **Type:** {doc['doc_type'] or 'unknown'}"
        + (f" &nbsp;|&nbsp; **Classification confidence:** {conf:.0%}" if conf is not None else "")
    )
    if doc.get("classification_reasoning"):
        st.caption(f"Classifier: {doc['classification_reasoning']}")

    # --- transient / error states -------------------------------------------
    if doc["status"] == "processing":
        with st.spinner("AI pipeline running (classify → extract → validate)…"):
            time.sleep(2)
        st.rerun()

    if doc["status"] == "failed":
        st.error(f"Processing failed: {doc.get('error')}")
        if st.button("Retry processing"):
            api("POST", f"/documents/{doc_id}/retry")
            st.rerun()
        return

    doctypes, _ = api("GET", "/doctypes")
    type_names = {t["name"]: t["display_name"] for t in (doctypes or [])}

    # --- unknown type: reviewer must classify manually ------------------------
    if doc["doc_type"] not in type_names:
        low_conf = conf is not None and conf < CONFIDENCE_THRESHOLD
        st.warning("The classifier could not confidently identify this document"
                   + (" (low confidence)." if low_conf else "."))
        chosen = st.selectbox("Set the document type", list(type_names),
                              format_func=type_names.get)
        if st.button("Classify & extract", type="primary"):
            with st.spinner("Extracting…"):
                _, err = api("POST", f"/documents/{doc_id}/classify",
                             json={"doc_type": chosen})
            if err:
                st.error(err)
            else:
                st.rerun()
        return

    # --- low-confidence classification banner ---------------------------------
    if conf is not None and conf < CONFIDENCE_THRESHOLD:
        st.warning(f"Classification confidence is below the {CONFIDENCE_THRESHOLD:.0%} "
                   "threshold — confirm the type before approving.")

    # --- re-classification -----------------------------------------------------
    with st.expander("Wrong type? Re-classify"):
        chosen = st.selectbox("Correct type", list(type_names), format_func=type_names.get)
        if st.button("Change type & re-extract"):
            with st.spinner("Re-extracting…"):
                _, err = api("POST", f"/documents/{doc_id}/classify", json={"doc_type": chosen})
            if err:
                st.error(err)
            else:
                st.rerun()

    left, right = st.columns([3, 2])

    with left:
        st.subheader("Extracted data")
        st.caption("Fields marked ⚠️ were extracted with low confidence — verify them "
                   "against the source document. Edit anything that is wrong.")
        edited = render_form(doc)

        save_col, approve_col = st.columns(2)
        with save_col:
            if st.button("💾 Save corrections", use_container_width=True):
                updated, err = api("PATCH", f"/documents/{doc_id}/data", json={"data": edited})
                if err:
                    st.error(err)
                else:
                    st.success("Corrections saved; validation re-ran.")
                    st.rerun()
        with approve_col:
            if st.button("✅ Approve", type="primary", use_container_width=True,
                         disabled=doc["status"] == "approved"):
                _, err = api("POST", f"/documents/{doc_id}/approve")
                if err:
                    if isinstance(err, dict):
                        st.error(err.get("message", "Approval blocked."))
                        show_issues(err.get("issues", []))
                    else:
                        st.error(err)
                else:
                    st.success("Approved.")
                    st.rerun()

    with right:
        st.subheader("Validation")
        show_issues(doc.get("validation_issues") or [])
        if doc.get("corrected_data") is not None:
            st.caption("This document has human corrections; the AI's original "
                       "output is preserved for comparison.")
            with st.expander("Original AI extraction"):
                st.json(doc.get("extracted_data"))

        st.subheader("Structured output")
        result, _ = api("GET", f"/documents/{doc_id}/result")
        if result:
            st.caption(f"As served to downstream systems: GET /documents/{doc_id}/result")
            st.json(result)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

PAGES = {"Upload": page_upload, "Documents": page_documents, "Review": page_review}

if "page" not in st.session_state:
    st.session_state["page"] = "Upload"

st.sidebar.title("📄 Document AI")
st.sidebar.caption("Upload → classify → extract → validate → review → approve")
page = st.sidebar.radio("Navigate", list(PAGES), index=list(PAGES).index(st.session_state["page"]))
st.session_state["page"] = page
PAGES[page]()

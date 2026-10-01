#!/usr/bin/env python3
"""
fetch_granola.py — pull every Granola meeting (full transcript + AI panels)
using an independent, automatically renewed login from granola_auth.py.

Usage:
    python3 fetch_granola.py

Output (written to ~/Library/Application Support/Granola/granola_export/):
    documents.json          — one entry per meeting (title, date, notes, summary panels)
    transcripts/<id>.json   — list of transcript segments (text, timestamps, speaker)
    summary.txt             — quick console summary

Safe to re-run. Refreshes changed meetings and retries empty cached responses.
"""

import gzip
import json
import os
import sys
import time
import urllib.request
import urllib.error
import zlib
from pathlib import Path
from granola_auth import AuthError, atomic_json, client_version, load_token

OUT = Path(os.environ.get("GRANOLA_EXPORT_DIR", str(Path.home() / "Library/Application Support/Granola/granola_export")))
TRANSCRIPTS_DIR = OUT / "transcripts"
PANELS_DIR = OUT / "panels"

API_BASE = "https://api.granola.ai"
UA = f"Granola/{client_version()} (local export)"


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _decode_body(raw, content_encoding):
    if not raw:
        return None
    enc = (content_encoding or "").lower()
    if enc == "gzip" or (len(raw) >= 2 and raw[:2] == b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    elif enc == "br":
        try:
            import brotli  # type: ignore
            raw = brotli.decompress(raw)
        except Exception:
            raise RuntimeError("response is brotli-encoded but `brotli` not installed; pip install brotli")
    return json.loads(raw)


def post(path, token, body, timeout=30):
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "User-Agent": UA,
            "X-Client-Version": client_version(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            enc = r.headers.get("Content-Encoding")
            return r.status, _decode_body(raw, enc)
    except urllib.error.HTTPError as e:
        raw = e.read()
        enc = e.headers.get("Content-Encoding") if hasattr(e, "headers") else None
        try:
            body = _decode_body(raw, enc)
        except Exception:
            body = raw.decode("utf-8", errors="replace")[:500]
        return e.code, {"_error": body}


def fetch_documents(token):
    """Pull all meeting documents.

    Granola's /v2/get-documents doesn't return a pagination cursor, but it
    *does* honor a high `limit` and supports `offset`. So we ask for a big
    page first; if it looks like we hit the cap (returned exactly PAGE),
    we keep paging via offset until a short page comes back.
    """
    PAGE = 1000  # Granola happily returns more than 100 if you ask
    all_docs = []
    seen = set()
    offset = 0
    for _ in range(50):  # hard cap (50 pages = 50k meetings)
        body = {"limit": PAGE, "offset": offset}
        status, resp = post("/v2/get-documents", token, body)
        if status in (404, 405):
            status, resp = post("/v1/get-documents", token, body)
        if status != 200:
            die(f"get-documents failed (HTTP {status}); keeping the existing export.")
        if not isinstance(resp, dict):
            die("Unexpected get-documents response; keeping the existing export.")
        docs = resp.get("docs", resp.get("documents"))
        if not isinstance(docs, list) or any(not isinstance(d, dict) or not d.get("id") for d in docs):
            die("Unexpected get-documents schema; keeping the existing export.")
        if not docs:
            break
        ids = {d["id"] for d in docs}
        if ids & seen or len(ids) != len(docs):
            die("Repeated documents in pagination; refusing an incomplete export.")
        seen.update(ids)
        all_docs.extend(docs)
        print(f"  fetched {len(all_docs)} so far (page returned {len(docs)})...")
        if len(docs) < PAGE:
            break  # short page = end of data
        offset += len(docs)
        time.sleep(0.2)
    else:
        die("Document pagination exceeded 50 pages; keeping the existing export.")
    return all_docs


def fetch_transcript(token, doc_id):
    status, resp = post("/v1/get-document-transcript", token, {"document_id": doc_id})
    if status != 200:
        return None, f"HTTP {status}: {str(resp)[:200]}"
    if not isinstance(resp, list):
        return None, "Unexpected transcript response (expected a list)"
    return resp, None


def fetch_panels(token, doc_id):
    """AI summary panels (the structured summary you see in the Granola UI)."""
    status, resp = post("/v1/get-document-panels", token, {"document_id": doc_id})
    if status != 200:
        return None, f"HTTP {status}: {str(resp)[:200]}"
    if not isinstance(resp, list):
        return None, "Unexpected panels response (expected a list)"
    return resp, None


def cache_current(path, doc, previous):
    if not path.exists():
        return False
    if doc.get("updated_at") != previous.get("updated_at"):
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    # A meeting can still be recording/generating when first fetched.
    return isinstance(data, list) and (bool(data) or time.time() - path.stat().st_mtime < 86400)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    PANELS_DIR.mkdir(exist_ok=True)

    try:
        token = load_token()
    except AuthError as exc:
        die(str(exc))

    print("\nFetching documents…")
    docs = fetch_documents(token)
    print(f"Got {len(docs)} documents.\n")

    previous_path = OUT / "documents.json"
    try:
        previous = {d["id"]: d for d in json.loads(previous_path.read_text())}
    except (OSError, ValueError, KeyError, TypeError):
        previous = {}

    success, skip, fail = 0, 0, 0
    failures = []

    for i, doc in enumerate(docs, 1):
        if doc.get("deleted_at") or doc.get("was_trashed"):
            continue
        doc_id = doc.get("id") or doc.get("document_id")
        title = (doc.get("title") or "(untitled)").strip()[:60]
        if not doc_id:
            print(f"[{i}/{len(docs)}] {title!r}: no id, skipping")
            fail += 1
            continue

        t_path = TRANSCRIPTS_DIR / f"{doc_id}.json"
        p_path = PANELS_DIR / f"{doc_id}.json"

        # Transcript
        if cache_current(t_path, doc, previous.get(doc_id, {})):
            t_status = "cached"
            skip += 1
        else:
            t, err = fetch_transcript(token, doc_id)
            if err:
                t_status = f"FAIL ({err})"
                fail += 1
                failures.append((doc_id, title, err))
            else:
                atomic_json(t_path, t)
                segs = len(t) if isinstance(t, list) else "?"
                t_status = f"{segs} segs"
                success += 1

        if not cache_current(p_path, doc, previous.get(doc_id, {})):
            p, _err = fetch_panels(token, doc_id)
            if p is not None:
                atomic_json(p_path, p)
            else:
                fail += 1
                failures.append((doc_id, title, f"Summary: {_err}"))

        print(f"[{i}/{len(docs)}] {title}: {t_status}")
        if t_status != "cached":
            time.sleep(0.3)  # gentle on the API

    print(f"\nDone. transcripts: {success} fetched, {skip} cached; {fail} fetch errors.")
    print(f"Output: {OUT}")
    if failures:
        print("\nFailures:")
        for fid, ftitle, ferr in failures[:20]:
            print(f"  {fid} {ftitle!r}: {ferr}")

    with open(OUT / "summary.txt", "w") as f:
        f.write(f"Granola export at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Documents: {len(docs)}\n")
        f.write(f"Transcripts new: {success}, cached: {skip}, failed: {fail}\n")

    if fail:
        die("Fetch incomplete; keeping the previous document index so failed updates are retried.")
    atomic_json(previous_path, docs)


if __name__ == "__main__":
    main()

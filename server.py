#!/usr/bin/env python3
"""ClaimProof working backend — Python standard library only.

Run:
    MODEL_API_KEY=<your $150-credit key> python3 server.py
    # then open http://127.0.0.1:8000

Storage (added Nov 3, 2026):
- SQLite database `claimproof.db` (one table, `claims`) for claim rows,
  totals, evidence and model notes. This replaces the original
  `claims.json` file; an existing claims.json is imported once on
  first run if the database is empty.
- Local folder `uploads/` for before/after photos, saved as files
  (not blobs in the DB, so they stay viewable and portable). Each file
  is named `<claim_id>_before.<ext>` / `<claim_id>_after.<ext>`,
  served back at GET /api/uploads/<filename>. Both the DB file and
  uploads/ are git-ignored: they are local state, not repo content.

Without MODEL_API_KEY the app still works end-to-end in local mode:
real parsing, matching, saved claims, and report export — but photo
verdicts stay Not Evidenced until a human (or, with a key, Wonder/SAM)
confirms them. Local mode is labelled in every response; it never
pretends to be a model result.

Endpoints:
    GET  /                     -> index.html
    GET  /api/health           -> {"live_models": bool, "storage": "sqlite+uploads", ...}
    POST /api/verify           -> JSON {scope, estimate, before?, after?,
                                        evidence?: {description: "confirms"|...}}
                                  before/after are data URLs (image/*).
    GET  /api/claims           -> saved claims (with before_url/after_url)
    GET  /api/claims/<id>      -> one claim
    POST /api/claims/<id>/evidence -> update evidence, recompute verdicts
    GET  /api/report/<id>      -> Markdown report download
    GET  /api/uploads/<file>   -> stored photo (path-traversal guarded)
"""
import base64
import json
import os
import re
import sqlite3
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import meta_api

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "claimproof.db")
UPLOADS = os.path.join(BASE, "uploads")
LEGACY_JSON = os.path.join(BASE, "claims.json")

ALLOWED_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                "image/gif": "gif"}


# ---------- parsing / matching (local, deterministic) ----------

def parse_lines(text):
    items = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        amount = 0.0
        if len(parts) > 1:
            m = re.search(r"[\d,]+(?:\.\d+)?", parts[1].replace("$", ""))
            amount = float(m.group(0).replace(",", "")) if m else 0.0
        items.append({"description": parts[0], "amount": amount})
    return items


def tokens(s):
    stop = {"and", "the", "north", "south", "east", "west", "a", "an", "of"}
    return [w for w in re.sub(r"[^a-z0-9 ]", "", s.lower()).split()
            if len(w) > 2 and w not in stop]


def overlap(a, b):
    A, B = set(tokens(a)), set(tokens(b))
    if not A or not B:
        return 0.0
    return len(A & B) / max(len(A), len(B))


def verdict_for(match, evidence):
    if not match:
        return "Out of Scope"
    return "Done" if evidence == "confirms" else "Not Evidenced"


def build_claim(scope_text, estimate_text, evidence=None, before=None, after=None,
                model_notes=None, before_path=None, after_path=None):
    evidence = evidence or {}
    scope = parse_lines(scope_text)
    estimate = parse_lines(estimate_text)
    rows = []
    for e in estimate:
        best, best_score = None, 0.0
        for s in scope:
            sc = overlap(e["description"], s["description"])
            if sc > best_score:
                best_score, best = sc, s
        match = best["description"] if best_score >= 0.4 else None
        ev = evidence.get(e["description"], "none")
        rows.append({"description": e["description"], "amount": e["amount"],
                     "match": match, "match_score": round(best_score, 2),
                     "evidence": ev, "verdict": verdict_for(match, ev)})
    totals = {
        "billed": sum(r["amount"] for r in rows),
        "verified": sum(r["amount"] for r in rows if r["verdict"] == "Done"),
        "flagged": sum(r["amount"] for r in rows if r["verdict"] != "Done"),
        "lines": len(rows),
        "verified_lines": sum(1 for r in rows if r["verdict"] == "Done"),
    }
    return {"id": uuid.uuid4().hex[:12], "scope": scope, "estimate": estimate,
            "rows": rows, "totals": totals, "has_before": bool(before_path or before),
            "has_after": bool(after_path or after),
            "before_url": f"/api/uploads/{before_path}" if before_path else None,
            "after_url": f"/api/uploads/{after_path}" if after_path else None,
            "model_notes": model_notes or [],
            "mode": "live" if meta_api.has_key() else "local"}


# ---------- persistence: SQLite + uploads/ ----------

def db():
    os.makedirs(UPLOADS, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS claims (
        id TEXT PRIMARY KEY, mode TEXT, scope TEXT, estimate TEXT,
        rows TEXT, totals TEXT, model_notes TEXT,
        before_path TEXT, after_path TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    # One-time import from the old claims.json, if present and DB is empty.
    if conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0 and os.path.exists(LEGACY_JSON):
        try:
            with open(LEGACY_JSON) as f:
                old = json.load(f)
            for c in (old.values() if isinstance(old, dict) else old):
                save_claim(c)
        except Exception:
            pass
    return conn


def _row_to_claim(row):
    claim = {"id": row[0], "mode": row[1],
             "scope": json.loads(row[2]), "estimate": json.loads(row[3]),
             "rows": json.loads(row[4]), "totals": json.loads(row[5]),
             "model_notes": json.loads(row[6]) if row[6] else [],
             "before_path": row[7], "after_path": row[8]}
    claim["has_before"] = bool(row[7])
    claim["has_after"] = bool(row[8])
    claim["before_url"] = f"/api/uploads/{row[7]}" if row[7] else None
    claim["after_url"] = f"/api/uploads/{row[8]}" if row[8] else None
    claim["created_at"] = row[9]
    return claim


def load_claims():
    conn = db()
    rows = conn.execute("SELECT id, mode, scope, estimate, rows, totals, model_notes, before_path, after_path, created_at FROM claims ORDER BY created_at").fetchall()
    conn.close()
    return {r[0]: _row_to_claim(r) for r in rows}


def get_claim(claim_id):
    conn = db()
    row = conn.execute("SELECT id, mode, scope, estimate, rows, totals, model_notes, before_path, after_path, created_at FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()
    return _row_to_claim(row) if row else None


def save_claim(claim):
    conn = db()
    conn.execute("""INSERT OR REPLACE INTO claims
        (id, mode, scope, estimate, rows, totals, model_notes, before_path, after_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (claim["id"], claim["mode"], json.dumps(claim["scope"]),
         json.dumps(claim["estimate"]), json.dumps(claim["rows"]),
         json.dumps(claim["totals"]), json.dumps(claim.get("model_notes") or []),
         (claim.get("before_url") or "").rsplit("/", 1)[-1] or None if claim.get("before_url") else claim.get("before_path"),
         (claim.get("after_url") or "").rsplit("/", 1)[-1] or None if claim.get("after_url") else claim.get("after_path")))
    conn.commit()
    conn.close()
    return claim


def save_upload(claim_id, kind, data_url):
    """Save a data-URL image to uploads/. Returns the filename, or None.

    Rejects non-image data URLs and anything that is not base64 data.
    A blob: URL from the browser is not a data URL and is rejected here;
    the frontend sends FileReader data URLs, so this path is the normal one.
    """
    if not data_url or not isinstance(data_url, str):
        return None
    m = re.match(r"data:(image/[a-z]+);base64,(.+)$", data_url, re.S)
    if not m or m.group(1) not in ALLOWED_MIME:
        return None
    ext = ALLOWED_MIME[m.group(1)]
    filename = f"{claim_id}_{kind}.{ext}"
    os.makedirs(UPLOADS, exist_ok=True)
    with open(os.path.join(UPLOADS, filename), "wb") as f:
        f.write(base64.b64decode(m.group(2)))
    return filename


def report_md(claim):
    lines = ["# ClaimProof report", "", f"Claim ID: `{claim['id']}`",
             f"Mode: {claim['mode']} (local = rule-based, no model verdicts)",
             "", "| Estimate line | Billed | Scope match | Verdict |",
             "|---|---|---|---|"]
    for r in claim["rows"]:
        lines.append(f"| {r['description']} | ${r['amount']:,.0f} | "
                     f"{r['match'] or '—'} | **{r['verdict']}** |")
    t = claim["totals"]
    lines += ["", f"**Totals:** Billed ${t['billed']:,.0f} · "
                  f"Verified ${t['verified']:,.0f} · Flagged ${t['flagged']:,.0f}",
                  f"Verified lines: {t['verified_lines']}/{t['lines']}"]
    if claim.get("before_url"):
        lines.append(f"Before photo: {claim['before_url']}")
    if claim.get("after_url"):
        lines.append(f"After photo: {claim['after_url']}")
    if claim.get("model_notes"):
        lines += ["", "## Model notes", ""] + [f"- {n}" for n in claim["model_notes"]]
    lines += ["", "_Low-confidence items stay flagged for a human; never auto-approved._"]
    return "\n".join(lines) + "\n"


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode() or "{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html")
        elif path == "/api/health":
            self._send(200, json.dumps({
                "live_models": meta_api.has_key(),
                "mode": "live" if meta_api.has_key() else "local",
                "models": meta_api.MODELS,
                "storage": "sqlite+uploads",
                "note": "Set MODEL_API_KEY to enable Spark/SAM/Wonder. "
                        "Without it, verdicts are rule-based + human photo review.",
            }))
        elif path == "/api/claims":
            self._send(200, json.dumps(list(load_claims().values())))
        elif path.startswith("/api/uploads/"):
            name = unquote(path.rsplit("/", 1)[-1])
            # Path-traversal guard: only a plain filename inside uploads/.
            if not name or "/" in name or "\\" in name or name.startswith("."):
                self._send(404, json.dumps({"error": "not found"}))
                return
            full = os.path.join(UPLOADS, name)
            if not os.path.isfile(full):
                self._send(404, json.dumps({"error": "not found"}))
                return
            ext = name.rsplit(".", 1)[-1].lower()
            ctype = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
                     "gif": "image/gif"}.get(ext, "application/octet-stream")
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)
        elif path.startswith("/api/claims/"):
            claim = get_claim(path.rsplit("/", 1)[-1])
            if not claim:
                self._send(404, json.dumps({"error": "claim not found"}))
            else:
                self._send(200, json.dumps(claim))
        elif path.startswith("/api/report/"):
            claim = get_claim(path.rsplit("/", 1)[-1])
            if not claim:
                self._send(404, json.dumps({"error": "claim not found"}))
            else:
                self._send(200, report_md(claim), "text/markdown")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/verify":
            body = self._json()
            notes = []
            scope_text, estimate_text = body.get("scope", ""), body.get("estimate", "")
            if meta_api.has_key():
                parsed = meta_api.spark_parse(scope_text, estimate_text)
                if parsed.get("error"):
                    notes.append(f"Spark error, local parser used: {parsed['error']}")
                else:
                    def fmt(items):
                        return "\n".join(
                            f"{i['description']} | {i['amount']}" for i in items)
                    scope_text = fmt(parsed.get("scope", []))
                    estimate_text = fmt(parsed.get("estimate", []))
                    notes.append("Scope/estimate parsed by Muse Spark (muse-spark-1.3).")
            # Claim id first, so uploads can be named after it.
            claim_id = uuid.uuid4().hex[:12]
            before_path = save_upload(claim_id, "before", body.get("before"))
            after_path = save_upload(claim_id, "after", body.get("after"))
            claim = build_claim(scope_text, estimate_text,
                                evidence=body.get("evidence"),
                                before=body.get("before"),
                                after=body.get("after"),
                                model_notes=notes,
                                before_path=before_path, after_path=after_path)
            claim["id"] = claim_id
            if meta_api.has_key() and body.get("before") and body.get("after"):
                for r in claim["rows"]:
                    if not r["match"]:
                        continue
                    j = meta_api.wonder_judge(body["before"], body["after"],
                                              r["description"])
                    if j.get("judgement"):
                        claim.setdefault("model_notes", []).append(
                            f"Wonder on '{r['description']}': {j['judgement']}")
                        if j["judgement"].startswith("CONFIRMED"):
                            r["evidence"], r["verdict"] = "confirms", "Done"
                    elif j.get("error"):
                        claim.setdefault("model_notes", []).append(
                            f"Wonder error on '{r['description']}': {j['error']}")
                t = claim["totals"]
                t["verified"] = sum(x["amount"] for x in claim["rows"] if x["verdict"] == "Done")
                t["flagged"] = sum(x["amount"] for x in claim["rows"] if x["verdict"] != "Done")
                t["verified_lines"] = sum(1 for x in claim["rows"] if x["verdict"] == "Done")
            save_claim(claim)
            self._send(200, json.dumps(claim))
        elif path.startswith("/api/claims/") and path.endswith("/evidence"):
            claim_id = path.split("/")[3]
            claim = get_claim(claim_id)
            if not claim:
                self._send(404, json.dumps({"error": "claim not found"}))
                return
            body = self._json()
            for r in claim["rows"]:
                if r["description"] in body.get("evidence", {}):
                    r["evidence"] = body["evidence"][r["description"]]
                    r["verdict"] = verdict_for(r["match"], r["evidence"])
            t = claim["totals"]
            t["verified"] = sum(x["amount"] for x in claim["rows"] if x["verdict"] == "Done")
            t["flagged"] = sum(x["amount"] for x in claim["rows"] if x["verdict"] != "Done")
            t["verified_lines"] = sum(1 for x in claim["rows"] if x["verdict"] == "Done")
            save_claim(claim)
            self._send(200, json.dumps(claim))
        else:
            self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    db()  # create tables / import legacy claims.json once, if present
    print(f"ClaimProof running at http://127.0.0.1:{port} "
          f"(mode: {'live' if meta_api.has_key() else 'local — set MODEL_API_KEY for Spark/SAM/Wonder'}, "
          f"storage: SQLite claimproof.db + uploads/)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

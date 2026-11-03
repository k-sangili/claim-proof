#!/usr/bin/env python3
"""ClaimProof working backend — Python standard library only.

Run:
    MODEL_API_KEY=<your $150-credit key> python3 server.py
    # then open http://127.0.0.1:8000

Without MODEL_API_KEY the app still works end-to-end in local mode:
real parsing, matching, saved claims, and report export — but photo
verdicts stay Not Evidenced until a human (or, with a key, Wonder/SAM)
confirms them. Local mode is labelled in every response; it never
pretends to be a model result.

Endpoints:
    GET  /                     -> index.html
    GET  /api/health           -> {"live_models": bool, ...}
    POST /api/verify           -> JSON {scope, estimate, before?, after?,
                                        evidence?: {description: "confirms"|...}}
    GET  /api/claims           -> saved claims
    GET  /api/claims/<id>      -> one claim
    POST /api/claims/<id>/evidence -> update evidence, recompute verdicts
    GET  /api/report/<id>      -> Markdown report download
"""
import base64
import html
import json
import os
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import meta_api

BASE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(BASE, "claims.json")


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
                model_notes=None):
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
            "rows": rows, "totals": totals, "has_before": bool(before),
            "has_after": bool(after), "model_notes": model_notes or [],
            "mode": "live" if meta_api.has_key() else "local"}


# ---------- persistence ----------

def load_claims():
    if not os.path.exists(STORE):
        return {}
    try:
        with open(STORE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_claim(claim):
    claims = load_claims()
    claims[claim["id"]] = claim
    with open(STORE, "w") as f:
        json.dump(claims, f, indent=2)
    return claim


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
    if claim.get("model_notes"):
        lines += ["", "## Model notes", ""] + [f"- {n}" for n in claim["model_notes"]]
    lines += ["", "_Low-confidence items stay flagged for a human; never auto-approved._"]
    return "\n".join(lines) + "\n"


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the console clean for demo

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
                "note": "Set MODEL_API_KEY to enable Spark/SAM/Wonder. "
                        "Without it, verdicts are rule-based + human photo review.",
            }))
        elif path == "/api/claims":
            self._send(200, json.dumps(list(load_claims().values())))
        elif path.startswith("/api/claims/"):
            claim = load_claims().get(path.rsplit("/", 1)[-1])
            if not claim:
                self._send(404, json.dumps({"error": "claim not found"}))
            else:
                self._send(200, json.dumps(claim))
        elif path.startswith("/api/report/"):
            claim = load_claims().get(path.rsplit("/", 1)[-1])
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
            # Live Spark parsing, when a key is present; on error, fall back
            # to local parsing and say so — never silently fake it.
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
            claim = build_claim(scope_text, estimate_text,
                                evidence=body.get("evidence"),
                                before=body.get("before"),
                                after=body.get("after"),
                                model_notes=notes)
            # Live visual judgement, one Wonder call per scope-matched line,
            # only when both photos are supplied and a key is present.
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
            claims = load_claims()
            claim = claims.get(claim_id)
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
            claims[claim_id] = claim
            with open(STORE, "w") as f:
                json.dump(claims, f, indent=2)
            self._send(200, json.dumps(claim))
        else:
            self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"ClaimProof running at http://127.0.0.1:{port} "
          f"(mode: {'live' if meta_api.has_key() else 'local — set MODEL_API_KEY for Spark/SAM/Wonder'})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

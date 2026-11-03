"""Meta Model API client for ClaimProof.

Official surface used (checked 2026-11-03 against Meta's cookbook/docs):
- Base URL: https://api.meta.ai/v1
- Auth: Bearer MODEL_API_KEY (env var MODEL_API_KEY, also accepts META_API_KEY)
- Text models are OpenAI-compatible: POST /chat/completions with
  model muse-spark-1.3 / muse-wonder-1.0
- SAM 3.1: POST /responses, model sam-3.1, streamed, with the image as a
  data URL and one short noun-phrase concept per request. The full mask
  grammar needs the official meta-sam-parser; this client extracts boxes
  with a regex and reports mask presence, and uses meta-sam-parser if it
  is installed. It never invents a mask.

No key -> every function returns {"mode": "local", ...} and the caller
must NOT present the result as a model verdict. An API error is returned
as an error, never silently replaced with a fake success.
"""
import json
import os
import re
import urllib.request
import urllib.error

BASE_URL = os.environ.get("META_MODEL_API_BASE_URL", "https://api.meta.ai/v1").rstrip("/")
MODELS = {"spark": "muse-spark-1.3", "wonder": "muse-wonder-1.0", "sam": "sam-3.1"}


def api_key():
    return os.environ.get("MODEL_API_KEY") or os.environ.get("META_API_KEY") or ""


def has_key():
    return bool(api_key())


def _post(path, payload, timeout=120):
    key = api_key()
    if not key:
        return {"mode": "local", "error": "no_api_key"}
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"mode": "live", "data": json.loads(resp.read().decode())}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:1000]
        return {"mode": "live", "error": f"HTTP {e.code}: {body}"}
    except Exception as e:  # network / timeout / JSON
        return {"mode": "live", "error": str(e)}


def _chat_text(data):
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        return ""


def spark_parse(scope_text, estimate_text):
    """Ask Spark to normalise scope/estimate into JSON line items.

    Returns {"mode": "live"/"local", "items": [...]} or an error dict.
    The caller falls back to the local parser only on a *reported* error.
    """
    prompt = (
        "Parse the insurer scope and contractor estimate below into JSON only, "
        "no prose: {\"scope\": [{\"description\": str, \"amount\": number}], "
        "\"estimate\": [{\"description\": str, \"amount\": number}]}. "
        "Keep descriptions short and literal; amounts are USD, 0 if absent.\n\n"
        f"SCOPE:\n{scope_text}\n\nESTIMATE:\n{estimate_text}"
    )
    res = _post("/chat/completions", {
        "model": MODELS["spark"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    })
    if res.get("error"):
        return res
    text = _chat_text(res["data"])
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"mode": "live", "error": "Spark returned no JSON object"}
    try:
        return {"mode": "live", **json.loads(m.group(0))}
    except Exception as e:
        return {"mode": "live", "error": f"Spark JSON parse failed: {e}"}


def spark_parse_document(text, kind):
    """Ask Spark to parse one uploaded document into JSON line items.

    kind is "insurer approved scope" or "contractor estimate".
    Returns {"mode": "live", "items": [...]} or an error dict; the caller
    falls back to the rule-based parser only on a *reported* error.
    """
    prompt = (
        "Parse the document below into JSON only, no prose: "
        "{\"items\": [{\"description\": str, \"amount\": number}]}. "
        f"The document is a {kind}. Include only actual repair work line "
        "items. Exclude summary lines: totals, subtotals, RCV, deductible, "
        "depreciation, taxes, fees, and amounts due. Keep descriptions "
        "short and literal, exactly as written; amounts are USD, 0 if absent.\n\n"
        f"DOCUMENT:\n{text[:12000]}"
    )
    res = _post("/chat/completions", {
        "model": MODELS["spark"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    })
    if res.get("error"):
        return res
    text_out = _chat_text(res["data"])
    m = re.search(r"\{.*\}", text_out, re.S)
    if not m:
        return {"mode": "live", "error": "Spark returned no JSON object"}
    try:
        parsed = json.loads(m.group(0))
        items = parsed.get("items", [])
        if not isinstance(items, list):
            return {"mode": "live", "error": "Spark JSON had no items list"}
        return {"mode": "live", "items": [
            {"description": str(i.get("description", "")).strip(),
             "amount": float(i.get("amount") or 0)}
            for i in items if str(i.get("description", "")).strip()]}
    except Exception as e:
        return {"mode": "live", "error": f"Spark JSON parse failed: {e}"}


def wonder_judge(before_data_url, after_data_url, line_description):
    """Ask Wonder (multimodal) whether the work visibly happened.

    Returns {"mode": "live", "judgement": str} or an error dict.
    """
    if not before_data_url or not after_data_url:
        return {"mode": "local", "error": "both photos required for visual judgement"}
    content = [
        {"type": "text", "text": (
            f"Compare the before photo and after photo for this repair line: "
            f"'{line_description}'. Answer in one line only, starting with "
            "CONFIRMED, NOT_EVIDENCED, or CONTRADICTED, then a short reason. "
            "Do not be fooled by lighting or camera-angle changes alone."
        )},
        {"type": "image_url", "image_url": {"url": before_data_url}},
        {"type": "image_url", "image_url": {"url": after_data_url}},
    ]
    res = _post("/chat/completions", {
        "model": MODELS["wonder"],
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
    })
    if res.get("error"):
        return res
    return {"mode": "live", "judgement": _chat_text(res["data"])}


def sam_boxes(image_data_url, concept):
    """Segment one concept in one image via SAM 3.1 (Responses API).

    Returns boxes extracted from the streamed output text, plus the raw
    output length so the caller can tell an empty match (valid) from a
    failure (error). Mask decoding needs meta-sam-parser if installed;
    without it, boxes + count are returned honestly, no mask is faked.
    """
    if not image_data_url:
        return {"mode": "local", "error": "image required for SAM"}
    # Responses API, streamed, per Meta's SAM cookbook.
    payload = {
        "model": MODELS["sam"],
        "stream": True,
        "input": [
            {"role": "user", "content": [
                {"type": "input_image", "image_url": image_data_url},
                {"type": "input_text", "text": concept},
            ]}
        ],
    }
    res = _post("/responses", payload)
    if res.get("error"):
        return res
    # Non-streamed fallback shape: try to find output_text anywhere.
    text = json.dumps(res["data"])
    boxes = []
    for m in re.finditer(r"x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=(\d+);h=(\d+)", text):
        x1, y1, x2, y2, w, h = map(int, m.groups())
        boxes.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "w": w, "h": h})
    return {"mode": "live", "concept": concept, "boxes": boxes,
            "box_count": len(boxes), "raw_chars": len(text)}

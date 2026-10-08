"""Benchmark receipt parsing across Bedrock and Anthropic API models against hand-checked ground truth.

Every model gets the same prompt as production (EXTRACTION_PROMPT) and the same
post-processing (_post_process), so only the model varies. Receipts are sent as a
upright JPEG to every model; the "app" row runs the app's own parser on the raw
stored file to check production behaviour end to end.

"api:" rows call the Anthropic API directly.
The key comes from ANTHROPIC_API_KEY, the repo's .env (gitignored), or else the app's SSM parameter.

--recheck applies the app's self-check to every row: if the items don't add up to the receipt's
SUBTOTAL / items-sold count, the model gets one follow-up (receipt_parser._RECHECK_PROMPT).

Usage: python experiments/parse_bench.py [--runs 2] [--models key1,key2] [--recheck]
Writes experiments/results/parse_<timestamp>.json and prints a summary table.
"""
import argparse
import base64
import copy
import difflib
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import boto3
import fitz
from PIL import Image, ImageOps
from botocore.config import Config

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from services import receipt_parser  # noqa: E402
from services.receipt_parser import EXTRACTION_PROMPT, _RECHECK_PROMPT, _post_process, _reconcile  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "results"

# USD per 1M tokens, US cross-region (us.*) on-demand standard tier.
# Nova/Claude from the AWS Pricing API (2026-10-05); GPT from AWS model cards / pricing announcements.
# "app" runs services/receipt_parser.py on the raw stored file, exactly as /api/upload does, so it
# tracks whatever model and preprocessing the app currently uses (price assumes Haiku 5.5 on the API).
# Every other row gets an upright JPEG with EXIF rotation applied.
# Before the 2026-10-05 fix the app sent raw sideways phone photos to Nova 2 Lite and scored ~15%.
MODELS = {
    "app":               (receipt_parser.MODEL_ID, 0.10, 0.50),
    "nova-2-lite":       ("us.amazon.nova-2-lite-v1:0", 0.33, 2.75),
    "nova-pro":          ("us.amazon.nova-pro-v1:0", 0.80, 3.20),
    "claude-haiku-4.5":  ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 1.10, 5.50),
    "claude-sonnet-4.6": ("us.anthropic.claude-sonnet-4-6", 3.30, 16.50),
    # Not enabled on this account yet (AccessDeniedException) -- run with --models once access is granted.
    "claude-sonnet-5.5": ("us.anthropic.claude-sonnet-5-5", 2.20, 11.00),
    "claude-haiku-5.5":  ("us.anthropic.claude-haiku-5-5", 0.11, 0.55),
    "gpt-6-luna":       ("us.openai.gpt-6-luna", 0.11, 0.55),
    "gpt-5.6-terra":     ("us.openai.gpt-5.6-terra", 2.20, 13.20),
    # Anthropic API (first-party), prices from Anthropic's model table (2026-10-06). Prompts up to
    # 100K tokens; a receipt is ~2-5K. No temperature is sent: the 5.x models reject non-default values.
    "api:haiku-5.5":     ("claude-haiku-5-5", 0.10, 0.50),
    "api:sonnet-5.5":    ("claude-sonnet-5-5", 2.00, 10.00),
    "api:sonnet-4.6":    ("claude-sonnet-4-6", 3.00, 15.00),
    "api:opus-5.5":      ("claude-opus-5-5", 4.00, 20.00),
}
DEFAULT_MODELS = ["app", "nova-2-lite", "nova-pro",
                  "claude-haiku-4.5", "claude-sonnet-4.6"]
MAX_TOKENS = {"nova-pro": 10000}

_bedrock = boto3.client("bedrock-runtime", region_name="us-east-1",
                        config=Config(read_timeout=600, retries={"max_attempts": 4, "mode": "adaptive"}))


def render_jpeg(path: Path, long_edge: int = 3000) -> bytes:
    """Upright JPEG that fits Bedrock's 3.75MB / 8000px image limits (applies EXIF rotation)."""
    if path.read_bytes()[:4] != b"%PDF":
        im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        im.thumbnail((long_edge, long_edge))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()
    doc = fitz.open(path)
    page = doc[0]
    zoom = long_edge / max(page.rect.width, page.rect.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    data = pix.tobytes("jpeg", jpg_quality=90)
    doc.close()
    return data


def extract_json(text: str) -> dict:
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


_usage = threading.local()
_app_create = receipt_parser._create


def _recording_create(messages):
    """Wraps the app's model call so we can total usage across its internal calls (incl. recheck)."""
    resp = _app_create(messages)
    _usage.totals["inputTokens"] += resp.usage.input_tokens
    _usage.totals["outputTokens"] += resp.usage.output_tokens
    return resp


receipt_parser._create = _recording_create


def call_app(path: Path) -> dict:
    """Run the app's parser on the stored file as-is, like /api/upload and /api/reparse."""
    data = path.read_bytes()
    _usage.totals = {"inputTokens": 0, "outputTokens": 0}
    t0 = time.time()
    if data[:4] == b"%PDF":
        parsed = receipt_parser.parse_receipt_pdf(data)
    else:
        parsed = receipt_parser.parse_receipt_image(data)
    return {"parsed": parsed, "usage": dict(_usage.totals), "seconds": round(time.time() - t0, 2)}


_anthropic = None
_anthropic_lock = threading.Lock()


def _anthropic_client():
    """Anthropic client: ANTHROPIC_API_KEY, else the repo's .env, else the app's SSM key."""
    global _anthropic
    with _anthropic_lock:
        if _anthropic is None:
            import anthropic
            key = os.environ.get("ANTHROPIC_API_KEY")
            env_file = ROOT.parent / ".env"
            if not key and env_file.exists():
                for line in env_file.read_text().splitlines():
                    name, _, value = line.partition("=")
                    if name.strip().removeprefix("export ").strip() == "ANTHROPIC_API_KEY":
                        key = value.strip().strip('"').strip("'")
            _anthropic = (anthropic.Anthropic(api_key=key, max_retries=4) if key
                          else receipt_parser._load_client())
        return _anthropic


def call(model_key: str, jpeg: bytes, followup: tuple | None = None) -> dict:
    """One request. followup=(previous call() result, user text) continues that conversation."""
    model_id = MODELS[model_key][0]
    t0 = time.time()
    if model_key.startswith("api:"):
        b64 = base64.standard_b64encode(jpeg).decode()
        messages = [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": EXTRACTION_PROMPT}]}]
        if followup:
            prev, text = followup
            # Replay the full assistant content (thinking blocks included) unchanged.
            messages += [{"role": "assistant", "content": prev["content"]},
                         {"role": "user", "content": [{"type": "text", "text": text}]}]
        resp = _anthropic_client().messages.create(model=model_id, max_tokens=16000, messages=messages)
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"stop_reason={resp.stop_reason}")
        return {"text": "".join(b.text for b in resp.content if b.type == "text"),
                "content": resp.content,
                "usage": {"inputTokens": resp.usage.input_tokens, "outputTokens": resp.usage.output_tokens},
                "seconds": round(time.time() - t0, 2)}

    block = {"image": {"format": "jpeg", "source": {"bytes": jpeg}}}
    messages = [{"role": "user", "content": [block, {"text": EXTRACTION_PROMPT}]}]
    if followup:
        prev, text = followup
        messages += [{"role": "assistant", "content": [{"text": prev["text"]}]},
                     {"role": "user", "content": [{"text": text}]}]
    req = dict(modelId=model_id, messages=messages,
               inferenceConfig={"maxTokens": MAX_TOKENS.get(model_key, 16000), "temperature": 0})
    try:
        resp = _bedrock.converse(**req)
    except _bedrock.exceptions.ValidationException as e:
        if "temperature" not in str(e).lower():
            raise
        req["inferenceConfig"].pop("temperature")  # some reasoning models reject it
        resp = _bedrock.converse(**req)
    text = "".join(c.get("text", "") for c in resp["output"]["message"]["content"])
    return {"text": text, "usage": resp["usage"], "seconds": round(time.time() - t0, 2)}


def parse_output(text: str) -> dict:
    parsed = extract_json(text)
    parsed["items"] = _post_process(copy.deepcopy(parsed.get("items", [])))
    return parsed


def call_with_recheck(model_key: str, jpeg: bytes, recheck: bool) -> tuple:
    """Mirror receipt_parser._extract: parse, then one follow-up if the totals don't reconcile."""
    out = call(model_key, jpeg)
    parsed = parse_output(out["text"])
    rechecked = False
    check = _reconcile(parsed) if recheck else None
    if check and check["error"] > 0.005:
        rechecked = True
        out2 = call(model_key, jpeg, followup=(out, _RECHECK_PROMPT.format(**check)))
        parsed2 = parse_output(out2["text"])
        check2 = _reconcile(parsed2)
        for k in ("inputTokens", "outputTokens"):
            out["usage"][k] += out2["usage"][k]
        out["seconds"] = round(out["seconds"] + out2["seconds"], 2)
        out["text"] += "\n\n--- recheck ---\n" + out2["text"]
        if check2 and check2["error"] < check["error"]:
            parsed = parsed2
    return out, parsed, rechecked


def item_sim(p: dict, t: dict) -> float:
    s = 0.0
    if p.get("item_number") == t["item_number"]:
        s += 0.5
    if _price(p.get("price")) == _price(t["price"]):
        s += 0.3
    s += 0.2 * difflib.SequenceMatcher(None, p.get("name", "").upper(), t["name"]).ratio()
    return s


def _price(v):
    try:
        return round(float(str(v).replace("$", "").rstrip("-")), 2)
    except (TypeError, ValueError):
        return None


def align(pred: list, truth: list) -> list:
    """Order-preserving alignment (Needleman-Wunsch, zero gap cost). Returns [(pi, ti)]."""
    n, m = len(pred), len(truth)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = max(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1] + item_sim(pred[i - 1], truth[j - 1]))
    pairs, i, j = [], n, m
    while i and j:
        if dp[i][j] == dp[i - 1][j - 1] + item_sim(pred[i - 1], truth[j - 1]):
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif dp[i][j] == dp[i - 1][j]:
            i -= 1
        else:
            j -= 1
    return [(a, b) for a, b in reversed(pairs) if item_sim(pred[a], truth[b]) >= 0.3]


def score(parsed: dict, gt: dict) -> dict:
    pred, truth = parsed.get("items", []), gt["items"]
    pairs = align(pred, truth)
    num_ok = sum(pred[a].get("item_number") == truth[b]["item_number"] for a, b in pairs)
    price_ok = sum(_price(pred[a].get("price")) == _price(truth[b]["price"]) for a, b in pairs)
    name_ok = sum(difflib.SequenceMatcher(None, pred[a].get("name", "").upper(), truth[b]["name"]).ratio() >= 0.8
                  for a, b in pairs)
    tpd_truth = [j for j, t in enumerate(truth) if t["tpd"]]
    tpd_ok = sum(1 for a, b in pairs if b in tpd_truth and pred[a].get("tpd")
                 and _price(pred[a].get("original_price")) == _price(truth[b]["original_price"]))
    paid_sum = round(sum(_price(p.get("price")) or 0 for p in pred), 2)
    n = len(truth)
    return {
        "date_ok": parsed.get("receipt_date") == gt["receipt_date"],
        "items_found": len(pairs), "items_truth": n,
        "extra_items": len(pred) - len(pairs),
        "item_number_ok": num_ok, "price_ok": price_ok, "name_ok": name_ok,
        "tpd_ok": tpd_ok, "tpd_truth": len(tpd_truth),
        "sum_matches_subtotal": paid_sum == _price(gt["subtotal"]),
        "paid_sum": paid_sum,
        # one headline number: share of (number, price, name) fields right, minus extras
        "field_accuracy": round(max(0, num_ok + price_ok + name_ok - len(pred) + len(pairs)) / (3 * n), 3),
        "perfect": (num_ok == price_ok == name_ok == n == len(pred) and tpd_ok == len(tpd_truth)
                    and parsed.get("receipt_date") == gt["receipt_date"]),
    }


def run_one(model_key, pdf_path, jpeg, gt, run_idx, recheck=False):
    rec = {"model": model_key, "receipt": gt["receipt_date"], "file": pdf_path.name, "run": run_idx}
    try:
        if model_key == "app":
            out = call_app(pdf_path)
            parsed = out["parsed"]  # already post-processed (and rechecked) by the app
        else:
            out, parsed, rec["rechecked"] = call_with_recheck(model_key, jpeg, recheck)
        _, in_price, out_price = MODELS[model_key]
        u = out["usage"]
        rec.update(seconds=out["seconds"], input_tokens=u["inputTokens"], output_tokens=u["outputTokens"],
                   cost_usd=round((u["inputTokens"] * in_price + u["outputTokens"] * out_price) / 1e6, 6),
                   raw=out.get("text", ""))
        rec["parsed"] = parsed
        rec["score"] = score(parsed, gt)
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"[:500]
    print(f"  done {model_key:<20} {pdf_path.name[:8]} run{run_idx} "
          f"{'ERROR ' + rec['error'][:80] if 'error' in rec else rec['score']['field_accuracy']}", flush=True)
    return rec


def summarize(records):
    """One row per model, pooled over every receipt/photo/run. Per-receipt accuracy on the right."""
    receipts = sorted({r["receipt"] for r in records})
    rows = {}
    for r in records:
        rows.setdefault(r["model"], []).append(r)
    rcols = "".join(f"{rc[5:]:>8}" for rc in receipts)
    hdr = (f"{'model':<26}{'ok':>6}{'acc':>6}{'date':>7}{'item#':>7}{'price':>7}{'name':>6}{'tpd':>7}"
           f"{'extra':>6}{'perfect':>8}{'sec':>6}{'$/rcpt':>9} |{rcols}")
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for model, rs in rows.items():
        ok = [r for r in rs if "score" in r]
        if not ok:
            print(f"{model:<26}{0:>3}/{len(rs):<2}  all failed: {rs[0].get('error', '')[:70]}")
            continue
        tot = lambda k: sum(r["score"][k] for r in ok)
        n_items = tot("items_truth")
        pct = lambda k: f"{tot(k) / n_items:.0%}"
        per = "".join(
            f"{sum(r['score']['field_accuracy'] for r in sub) / len(sub):>8.0%}" if sub else f"{'err':>8}"
            for sub in ([r for r in ok if r["receipt"] == rc] for rc in receipts))
        print(f"{model:<26}{len(ok):>3}/{len(rs):<2}{tot('field_accuracy') / len(ok):>6.0%}"
              f"{tot('date_ok'):>4}/{len(ok):<2}{pct('item_number_ok'):>7}{pct('price_ok'):>7}{pct('name_ok'):>6}"
              f"{tot('tpd_ok'):>4}/{tot('tpd_truth'):<2}{tot('extra_items') / len(ok):>6.1f}"
              f"{tot('perfect'):>5}/{len(ok):<2}{sum(r['seconds'] for r in ok) / len(ok):>6.1f}"
              f"{sum(r['cost_usd'] for r in ok) / len(ok):>9.4f} |{per}")
    errs = {}
    for r in records:
        if "error" in r:
            errs.setdefault((r["model"], r["error"][:160]), 0)
            errs[(r["model"], r["error"][:160])] += 1
    for (m, e), c in errs.items():
        print(f"  {c}x {m}: {e}")
    rechecks = {m: sum(bool(r.get("rechecked")) for r in rs) for m, rs in rows.items()}
    if any(rechecks.values()):
        print("  rechecks fired: " + ", ".join(f"{m} {c}/{len(rows[m])}" for m, c in rechecks.items()))
    spend = {}
    for r in records:
        backend = "Anthropic API" if r["model"].startswith("api:") or r["model"] == "app" else "Bedrock"
        spend[backend] = spend.get(backend, 0) + r.get("cost_usd", 0)
    print("  spend this run: " + ", ".join(f"{b} ${v:.4f}" for b, v in spend.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--recheck", action="store_true",
                    help="apply the app's subtotal/item-count self-check (one follow-up call) to every row")
    ap.add_argument("--truth", nargs="*", default=sorted(str(f) for f in (DATA / "ground_truth").glob("*.json")),
                    help="ground-truth files (default: all in data/ground_truth/)")
    args = ap.parse_args()

    truths = [json.loads(Path(t).read_text()) for t in args.truth]
    models = [m.strip() for m in args.models.split(",")]
    unknown = [m for m in models if m not in MODELS]
    if unknown:
        sys.exit(f"unknown model(s): {', '.join(unknown)}; choose from: {', '.join(MODELS)}")
    files = [(DATA / "files" / f, gt) for gt in truths for f in gt["files"]]
    jpegs = {f: render_jpeg(f) for f, _ in files}

    jobs = [(m, f, jpegs[f], gt, k, args.recheck) for m in models for f, gt in files for k in range(args.runs)]
    print(f"{len(jobs)} calls: {len(models)} models x {len(files)} photos ({len(truths)} receipts) x {args.runs} runs"
          f"{' (+ recheck when totals disagree)' if args.recheck else ''}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda j: run_one(*j), jobs))

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"parse_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(records, indent=2, default=str))
    summarize(records)
    print(f"\nfull results: {out}")


if __name__ == "__main__":
    main()

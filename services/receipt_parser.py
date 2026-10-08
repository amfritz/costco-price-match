import base64
import io
import json
import logging
import re
import os
import threading
import time
from datetime import datetime, timezone

import anthropic
import boto3
import fitz
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# Chosen by experiments/parse_bench.py (2026-10-07): Haiku 5.5 parsed every benchmark receipt
# perfectly (15/15 runs, no rechecks needed) at ~0.13c a receipt; Sonnet 4.6 on Bedrock managed
# 13/15 at ~2.6c. Called on the Anthropic API because Bedrock has not enabled 5.x for this account.
MODEL_ID = "claude-haiku-5-5"
# 30-day key kept in SSM so it can be rotated without a redeploy.
API_KEY_PARAM = os.environ.get("ANTHROPIC_KEY_PARAM", "/costco-scanner/anthropic-api-key")
KEY_ROTATE_WARN_DAYS = 25
# 3000px keeps receipt text sharp and well under the API's 8000px / 5MB per-image limits.
_MAX_EDGE = 3000
_MAX_PDF_PAGES = 20
# API Gateway cuts requests off at 30s; give up sooner so the UI gets a readable error.
_TIMEOUT_SECONDS = 25

_ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
_client = None
_client_lock = threading.Lock()
_status_ok_at = 0.0

EXTRACTION_PROMPT = """Extract all lines from this Costco receipt as items.
Return ONLY valid JSON with this exact structure, no other text:
{
  "store": "store location or number",
  "receipt_date": "YYYY-MM-DD",
  "subtotal": "123.45",
  "items_sold": "17",
  "items": [
    {"name": "ITEM NAME", "price": "12.99", "qty": "1", "item_number": "1234567"}
  ]
}
Rules:
- Include EVERY line as a separate item, including TPD lines
- TPD lines should have name like "TPD/SHOES" or "TPD/3333332" exactly as shown
- Price should be a string with 2 decimals. If price ends with "-" on receipt, include the minus sign (e.g. "10.00-")
- qty defaults to "1" if not shown
- item_number = the number shown before the item name on that line. Empty string if not visible.
- Do NOT merge or combine any lines
- Do NOT skip any lines
- Ignore tax lines, subtotals, totals, payment lines
- receipt_date should be extracted from the receipt date field
- subtotal = the SUBTOTAL amount; items_sold = the "TOTAL NUMBER OF ITEMS SOLD" / "Items Sold" count. Empty string if not shown.
- The same item can appear on several consecutive identical lines; each one is a separate purchase and must be listed"""

_RECHECK_PROMPT = """Your items do not reconcile with the receipt: they add up to ${got_sum} across {got_count} items, but the receipt shows SUBTOTAL ${subtotal} and {items_sold} items sold.
Re-read the receipt line by line, top to bottom. Common causes: a repeated identical line was listed once, a line was skipped, a digit was misread, or a discount line ("/ 1234567" with a price ending in "-") was given without its minus sign. Return the complete corrected JSON in the same format."""

_NOISE_PATTERNS = re.compile(
    r"^(AGE\s*VERIFIED|DEPOSIT|L\d+\s*MEMBER|N\d+\s*MEMBER|\d+\s*@\s*[\d.]+)",
    re.IGNORECASE,
)


def _prepare_image(img_bytes: bytes) -> bytes:
    """Return an upright, downscaled JPEG.

    Phone photos are stored sideways with an EXIF orientation tag; viewers rotate them but
    the model sees the raw pixels, which made parsing accuracy collapse (~15% vs ~98% upright).
    """
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
    im.thumbnail((_MAX_EDGE, _MAX_EDGE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _pdf_to_images(pdf_bytes: bytes) -> list:
    """Render each PDF page to a JPEG (scanned receipts have no text layer to read)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in list(doc)[:_MAX_PDF_PAGES]:
        zoom = min(_MAX_EDGE / max(page.rect.width, page.rect.height), 300 / 72)
        images.append(page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("jpeg", jpg_quality=90))
    doc.close()
    return images


class ParserUnavailable(Exception):
    """Parsing can't run at all (key rejected, no credit, outage). The message is shown in the UI."""


def _load_client(refresh: bool = False) -> anthropic.Anthropic:
    """Anthropic client built from the SSM key, cached per Lambda container.

    refresh=True re-reads SSM, so a key rotated in Parameter Store is picked up without a redeploy.
    """
    global _client
    with _client_lock:
        if _client is None or refresh:
            try:
                key = _ssm.get_parameter(Name=API_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
            except _ssm.exceptions.ParameterNotFound:
                raise ParserUnavailable(f"Anthropic API key not found: SSM parameter {API_KEY_PARAM} does not exist.")
            except Exception as e:
                raise ParserUnavailable(f"Could not read the Anthropic API key from SSM ({API_KEY_PARAM}): {e}")
            _client = anthropic.Anthropic(api_key=key.strip(), timeout=_TIMEOUT_SECONDS, max_retries=1)
        return _client


def _friendly(e: anthropic.APIError) -> ParserUnavailable:
    """Turn an API failure into a message that says what to fix."""
    if isinstance(e, anthropic.AuthenticationError):
        msg = (f"The Anthropic API key was rejected (expired, revoked, or mistyped). Create a new key "
               f"and update SSM parameter {API_KEY_PARAM}.")
    elif getattr(e, "status_code", None) == 402 or "credit balance" in str(e).lower():
        msg = "The Anthropic account is out of credit. Add credit at console.anthropic.com."
    elif isinstance(e, anthropic.PermissionDeniedError):
        msg = f"The Anthropic API key is not allowed to use {MODEL_ID}."
    elif isinstance(e, anthropic.NotFoundError):
        msg = f"Model {MODEL_ID} is not available to this Anthropic account."
    elif isinstance(e, (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.RateLimitError,
                        anthropic.InternalServerError)):
        msg = "The Anthropic API is unavailable or overloaded right now. Try again in a few minutes."
    else:
        msg = f"Anthropic API error ({getattr(e, 'status_code', '?')}): {getattr(e, 'message', e)}"
    log.error(f"Receipt parser unavailable: {msg} [{type(e).__name__}: {e}]")
    return ParserUnavailable(msg)


def _with_client(fn):
    """Run fn(client); on a rejected key, re-read SSM once in case the key was rotated."""
    for refresh in (False, True):
        try:
            return fn(_load_client(refresh=refresh))
        except anthropic.AuthenticationError as e:
            if refresh:
                raise _friendly(e) from e
        except anthropic.APIError as e:
            raise _friendly(e) from e


def _create(messages: list):
    return _with_client(lambda c: c.messages.create(model=MODEL_ID, max_tokens=16000, messages=messages))


def status() -> dict:
    """Health check for the UI banner: validates the key (Models API, no tokens billed) and its age."""
    global _status_ok_at
    result = {"ok": True, "problem": "", "model": MODEL_ID, "key_param": API_KEY_PARAM,
              "key_age_days": None, "rotate_soon": False}
    try:
        modified = _ssm.get_parameter(Name=API_KEY_PARAM)["Parameter"]["LastModifiedDate"]
        result["key_age_days"] = (datetime.now(timezone.utc) - modified).days
        result["rotate_soon"] = result["key_age_days"] >= KEY_ROTATE_WARN_DAYS
    except Exception:
        pass  # a missing or unreadable parameter is reported by _load_client below
    if time.time() - _status_ok_at < 300:
        return result  # key validated in the last 5 minutes
    try:
        _with_client(lambda c: c.models.retrieve(MODEL_ID))
        _status_ok_at = time.time()
    except ParserUnavailable as e:
        result.update(ok=False, problem=str(e))
    return result


def _converse(messages: list) -> tuple:
    response = _create(messages)
    if response.stop_reason in ("refusal", "max_tokens"):
        raise ValueError(f"Model stopped early: {response.stop_reason}")
    text = "".join(b.text for b in response.content if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    result = json.loads(text[start:end + 1])
    result["items"] = _post_process(result.get("items", []))
    return response.content, result


def _reconcile(result: dict) -> dict | None:
    """Compare parsed items with the receipt's own SUBTOTAL and items-sold count.

    Returns None when the receipt didn't show both, else the numbers plus an error score
    (0 = items match the receipt exactly).
    """
    try:
        subtotal = float(str(result.get("subtotal", "")).replace("$", "").replace(",", ""))
        items_sold = int(str(result.get("items_sold", "")).strip())
    except ValueError:
        return None
    got_sum = round(sum(float(i["price"]) for i in result["items"] if i.get("price")), 2)
    got_count = sum(int(i.get("qty") or 1) for i in result["items"])
    return {"got_sum": f"{got_sum:.2f}", "got_count": got_count,
            "subtotal": f"{subtotal:.2f}", "items_sold": items_sold,
            "error": abs(got_sum - subtotal) + abs(got_count - items_sold)}


def _extract(images: list) -> dict:
    """Parse the receipt images, then self-check against the printed subtotal and item count.

    If the items don't add up, ask the model once more with the discrepancy spelled out
    (models tend to collapse runs of identical lines, e.g. 4x the same item listed as 3).
    """
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.standard_b64encode(img).decode()}}
               for img in images]
    messages = [{"role": "user", "content": content + [{"type": "text", "text": EXTRACTION_PROMPT}]}]
    reply, result = _converse(messages)
    check = _reconcile(result)
    if check and check["error"] > 0.005:
        # Replay the assistant reply unchanged (thinking blocks included).
        messages += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": [{"type": "text", "text": _RECHECK_PROMPT.format(**check)}]}]
        try:
            _, retry = _converse(messages)
            retry_check = _reconcile(retry)
            if retry_check and retry_check["error"] < check["error"]:
                result, check = retry, retry_check
        except Exception as e:
            log.warning(f"Receipt recheck failed: {e}")
    result["verified"] = bool(check) and check["error"] <= 0.005
    if check and not result["verified"]:
        log.warning(f"Receipt does not reconcile after recheck: {check}")
    return result


def _post_process(items: list) -> list:
    """Filter noise, then merge TPD discount lines into their preceding item."""
    cleaned = []
    pending_qty = None
    for item in items:
        name = item.get("name", "").strip()
        price_str = item.get("price", "0").strip()

        qty_match = re.match(r"^(\d+)\s*@\s*[\d.]+", name)
        if qty_match:
            pending_qty = qty_match.group(1)
            continue

        if _NOISE_PATTERNS.match(name):
            continue
        if "TPD/" not in name.upper() and (not price_str or price_str in ("0", "0.00", "")):
            continue

        if pending_qty and "TPD/" not in name.upper():
            item["qty"] = pending_qty
            pending_qty = None

        cleaned.append(item)

    merged = []
    for item in cleaned:
        name = item.get("name", "")
        price_str = item.get("price", "0").strip()
        clean_price = price_str.rstrip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ @#*")
        is_tpd = "TPD/" in name.upper()
        is_negative = clean_price.endswith("-")
        # Costco discount lines read "/ <item#>" pointing at the line above. Models sometimes drop
        # the trailing "-", so a line that is only a reference to the previous item is a discount too.
        ref = re.fullmatch(r"(?:TPD)?\s*/?\s*(\d{4,8})", name.strip(), re.IGNORECASE)
        refs_prev = bool(ref and merged and ref.group(1) == merged[-1].get("item_number"))

        if (is_tpd or is_negative or refs_prev) and merged:
            prev = merged[-1]
            try:
                discount = float(clean_price.replace("-", ""))
                orig = float(prev["price"])
                if discount < orig:
                    prev["original_price"] = prev["price"]
                    prev["price"] = f"{orig - discount:.2f}"
                    prev["tpd"] = True
            except ValueError:
                pass
            continue

        item["price"] = clean_price.replace("-", "")
        item.setdefault("tpd", False)
        item.setdefault("original_price", "")

        try:
            q = int(item.get("qty", "1"))
            p = float(item["price"])
            if q > 1 and abs(p / q - round(p / q, 2)) > 0.001:
                item["qty"] = "1"
        except (ValueError, ZeroDivisionError):
            pass

        n = item.get("name", "")
        num = item.get("item_number", "")
        if not num:
            m = re.match(r"^([\dOoBbIlSsGg]{4,8})\s+", n)
            if m:
                raw = m.group(1)
                fixed = raw.translate(str.maketrans("OoBbIlSsGg", "0088115599"))
                if fixed.isdigit():
                    num = fixed
                    item["item_number"] = num
                    item["name"] = n[len(raw):].strip()
                    n = item["name"]
        if num and len(num) > 8:
            item["item_number"] = ""
            num = ""
        if num and n.startswith(num):
            item["name"] = n[len(num):].strip()
        merged.append(item)
    return merged


def parse_receipt_image(img_bytes: bytes) -> dict:
    """Parse a receipt photo (JPG/PNG/WebP/GIF)."""
    return _extract([_prepare_image(img_bytes)])


def parse_receipt_pdf(pdf_bytes: bytes) -> dict:
    """Parse a receipt PDF by rendering its pages to images."""
    return _extract(_pdf_to_images(pdf_bytes))

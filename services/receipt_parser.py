import boto3
import io
import json
import re
import os

import fitz
from botocore.config import Config
from PIL import Image, ImageOps

_bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                        config=Config(read_timeout=120))
# Chosen by experiments/parse_bench.py: Sonnet 4.6 was the only model to get every price,
# date and TPD right on the benchmark receipts (Nova 2 Lite misread digits, Haiku skipped TPDs).
MODEL_ID = "us.anthropic.claude-sonnet-4-6"
# Bedrock rejects images over 8000px or 3.75MB; 3000px keeps receipt text sharp and well under both.
_MAX_EDGE = 3000
_MAX_PDF_PAGES = 20  # Converse accepts at most 20 images per request

EXTRACTION_PROMPT = """Extract all lines from this Costco receipt as items.
Return ONLY valid JSON with this exact structure, no other text:
{
  "store": "store location or number",
  "receipt_date": "YYYY-MM-DD",
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
- receipt_date should be extracted from the receipt date field"""

_NOISE_PATTERNS = re.compile(
    r"^(AGE\s*VERIFIED|DEPOSIT|L\d+\s*MEMBER|N\d+\s*MEMBER|\d+\s*@\s*[\d.]+)",
    re.IGNORECASE,
)


def _prepare_image(img_bytes: bytes) -> bytes:
    """Return an upright, downscaled JPEG.

    Phone photos are stored sideways with an EXIF orientation tag; viewers rotate them but
    Bedrock sees the raw pixels, which made parsing accuracy collapse (~15% vs ~98% upright).
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


def _extract(images: list) -> dict:
    """Single Converse call over one or more receipt images, then shared post-processing."""
    content = [{"image": {"format": "jpeg", "source": {"bytes": img}}} for img in images]
    response = _bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": content + [{"text": EXTRACTION_PROMPT}]}],
        inferenceConfig={"maxTokens": 8192, "temperature": 0},
    )
    text = "".join(c.get("text", "") for c in response["output"]["message"]["content"])
    start, end = text.find("{"), text.rfind("}")
    result = json.loads(text[start:end + 1])
    result["items"] = _post_process(result.get("items", []))
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

        if (is_tpd or is_negative) and merged:
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

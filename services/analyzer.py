"""Price-adjustment matching: which receipt items are on sale now, and how much is owed back.

Code picks candidate deals for each receipt item and does all the arithmetic. Claude Haiku 5.5
answers one question only, for candidates that don't share an item number: is this deal for the
same product? Deals that match on item number need no model call.
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from pydantic import BaseModel

from services import db, receipt_parser

log = logging.getLogger(__name__)

MODEL_ID = receipt_parser.MODEL_ID
# Costco adjusts the price if it drops within 30 days of purchase.
ADJUST_DAYS = 30
# Forum and blog deals carry no end date; treat them as running this long after they were posted.
UNDATED_DEAL_DAYS = 14
MAX_CANDIDATES = 8
# Items per model call; batches run in parallel so analysis finishes inside API Gateway's 30s.
BATCH_SIZE = 40

_STOP_TOKENS = {
    "the", "and", "for", "with", "pack", "pk", "ct", "count", "oz", "lb", "lbs", "size", "sizes", "plus",
    "mens", "womens", "kids", "item", "new", "each", "set", "ks", "kirkland", "signature", "costco",
}

SYSTEM_PROMPT = """You check price-match candidates for Costco receipts. For each receipt item you get the
deals whose names share words with it. Decide which deal, if any, is for the SAME product the member bought.

Receipt names are the register's truncated abbreviations: "KS" = Kirkland Signature, "TORT CHIP" = tortilla
chips, "ORG" = organic, "GRK YOG" = Greek yogurt. Expand them before comparing.

A deal matches only if it is the same brand, the same kind of product and a compatible variety and size.
- Kirkland Signature (KS) is Costco's own brand: a KS item never matches a deal for another brand, and a
  branded item never matches a Kirkland deal.
- Sharing a word is not enough: "KS TORT CHIP" is not "Siete Grain-Free Tortilla Chips".
- A deal price far below what was paid usually means a different product or a smaller size.
- If you cannot tell, it is not a match. A missed match costs the member little; a wrong one sends them to
  the membership counter for nothing.

Return one verdict for every receipt item, using the ids given: give your reason, then same_product, then the
matching deal id. When nothing matches, same_product is false and deal is ""."""


class Verdict(BaseModel):
    # Field order is generation order: reason first, then the decision it supports.
    item: str
    reason: str
    same_product: bool
    deal: str


class Verdicts(BaseModel):
    verdicts: list[Verdict]


def _num(s):
    try:
        return float(str(s).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _day(s):
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _deal_overlaps(deal: dict, bought: date) -> bool:
    """The deal ran at some point in the 30 days after the purchase."""
    start = _day(deal.get("promo_start")) or _day(deal.get("scanned_date"))
    if not start:
        return False
    end = _day(deal.get("promo_end")) or start + timedelta(days=UNDATED_DEAL_DAYS)
    return start <= bought + timedelta(days=ADJUST_DAYS) and end >= bought


def _deal_numbers(deal: dict) -> set:
    return {n.strip().lstrip("0") for n in str(deal.get("item_number", "")).split(",") if n.strip()}


def _tokens(name: str) -> list:
    return [t for t in re.split(r"[^a-z0-9]+", name.lower())
            if len(t) >= 3 and not t.isdigit() and t not in _STOP_TOKENS]


def _name_score(item_tokens: list, deal_tokens: list) -> int:
    """Receipt names are truncations ("TORT" for tortilla), so a prefix of a deal word counts."""
    return sum(1 for t in item_tokens if any(d.startswith(t) or (len(d) >= 4 and t.startswith(d)) for d in deal_tokens))


def _savings(item: dict, deal: dict):
    """What the member is owed: paid minus the sale price, or the discount if they paid full price."""
    paid = item["unit_price"]
    sale, off = _num(deal.get("sale_price")), _num(deal.get("discount"))
    if sale is not None and sale > 0:
        owed = paid - sale
    elif off is not None and not item["tpd"]:
        owed = min(off, paid)
    else:
        return None
    return round(owed, 2) if owed >= 0.01 else None


def _filter_deals(drops: list, date_from=None, date_to=None, sources=None) -> list:
    """Filter deals by date range and sources."""
    filtered = drops
    if sources:
        src_set = set(sources)
        filtered = [d for d in filtered if d.get("source", "") in src_set]
    if date_from:
        filtered = [d for d in filtered if (d.get("promo_end") or d.get("scanned_date", "")[:10]) >= date_from]
    if date_to:
        filtered = [d for d in filtered if (d.get("scanned_date", "")[:10] or d.get("promo_end", "9999")) <= date_to]
    return filtered


def _receipt_items(receipt_ids=None) -> list:
    if receipt_ids:
        receipts = [r for r in (db.get_receipt(rid) for rid in receipt_ids) if r]
    else:
        receipts = db.get_recent_receipts(30)
    items = []
    for r in receipts:
        for item in r.get("items", []):
            price, qty = _num(item.get("price")), _num(item.get("qty")) or 1
            if price is None or price <= 0 or str(item.get("price", "")).endswith("-"):
                continue  # discount and refund lines
            items.append({
                "id": f"r{len(items)}",
                "name": item["name"],
                "item_number": str(item.get("item_number", "")).strip().lstrip("0"),
                "unit_price": round(price / qty, 2) if qty > 1 else price,
                "original_price": item.get("original_price", ""),
                "tpd": bool(item.get("tpd")),
                "receipt_date": r.get("receipt_date", ""),
                "receipt_id": r["receipt_id"],
            })
    return items


def _candidates(items: list, deals: list) -> tuple:
    """Split into (item-number matches, {item id: [name-similar deals]}) within each purchase's window."""
    deal_tokens = [_tokens(d["item_name"]) for d in deals]
    by_number, by_name = [], {}
    for item in items:
        bought = _day(item["receipt_date"])
        if not bought:
            continue
        live = [(i, d) for i, d in enumerate(deals) if _deal_overlaps(d, bought)]
        exact = [d for _, d in live if item["item_number"] and item["item_number"] in _deal_numbers(d)]
        owed = [(s, d) for d in exact if (s := _savings(item, d))]
        if owed:
            savings, deal = max(owed, key=lambda x: x[0])
            by_number.append({"item": item, "deal": deal, "savings": savings, "matched_by": "Item #"})
            continue
        if exact:
            continue  # same item number, but no money owed: no need to look at name matches
        toks = _tokens(item["name"])
        # A deal listing other item numbers is a different product, however alike the names are.
        scored = [(_name_score(toks, deal_tokens[i]), d) for i, d in live
                  if _savings(item, d) and not (item["item_number"] and _deal_numbers(d))]
        scored = sorted((x for x in scored if x[0] > 0), key=lambda x: -x[0])[:MAX_CANDIDATES]
        if scored:
            by_name[item["id"]] = [d for _, d in scored]
    return by_number, by_name


def _format_deal(d: dict) -> str:
    sale, off = d.get("sale_price"), d.get("discount")
    price = f"${sale}" if sale else ""
    if off:
        price += f" (${off} off)" if price else f"${off} off"
    return f'"{d["item_name"]}" {price}, {d.get("source", "")}'


def _judge_batch(batch: list) -> list:
    lines = []
    for item, deals in batch:
        lines.append(f'{item["id"]}: "{item["name"]}" paid ${item["unit_price"]:.2f} on {item["receipt_date"]}')
        for j, d in enumerate(deals):
            lines.append(f'  {item["id"]}d{j}: {_format_deal(d)}')
    response = receipt_parser.with_client(lambda c: c.messages.parse(
        model=MODEL_ID,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        output_format=Verdicts,
    ))
    if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
        raise RuntimeError(f"Deal matching stopped early ({response.stop_reason})")
    return response.parsed_output.verdicts


def _judge(items: list, by_name: dict) -> list:
    """Ask the model which name-similar candidate, if any, is the same product."""
    pending = [(item, by_name[item["id"]]) for item in items if item["id"] in by_name]
    if not pending:
        return []
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    with ThreadPoolExecutor(max_workers=len(batches)) as pool:
        verdicts = [v for vs in pool.map(_judge_batch, batches) for v in vs]

    lookup = {f'{item["id"]}d{j}': (item, d) for item, deals in pending for j, d in enumerate(deals)}
    matches = []
    for v in verdicts:
        if not v.same_product or v.deal not in lookup or not v.deal.startswith(v.item + "d"):
            continue
        item, deal = lookup[v.deal]
        log.info(f'Name match: {item["name"]} -> {deal["item_name"]} ({v.reason})')
        matches.append({"item": item, "deal": deal, "savings": _savings(item, deal), "matched_by": "Name"})
    return matches


def _cell(s) -> str:
    return str(s).replace("|", "/").replace("\n", " ").strip()


def _item_link(item: dict) -> str:
    return f'[{_cell(item["name"])}](/api/receipt/{item["receipt_id"]}/pdf)'


def _source_link(deal: dict) -> str:
    src = _cell(deal.get("source", ""))
    link = deal.get("link", "")
    return f"[{src}]({link})" if link.startswith("http") else src


def _match_cell(m: dict) -> str:
    """Name matches show the deal's own name so the member can check it is the same product."""
    return "Item #" if m["matched_by"] == "Item #" else f'Name: {_cell(m["deal"]["item_name"])}'


def _report(matches: list, items: list) -> str:
    out = ["## 💰 Price Adjustment Opportunities", ""]
    adjustable = sorted((m for m in matches if not m["item"]["tpd"]),
                        key=lambda m: m["item"]["receipt_date"], reverse=True)
    if adjustable:
        out += ["| Item | Item # | Date | Paid | Sale Price | Savings | Match | Source |",
                "|---|---|---|---|---|---|---|---|"]
        for m in adjustable:
            item, deal = m["item"], m["deal"]
            sale = f'${deal["sale_price"]}' if deal.get("sale_price") else f'${deal.get("discount")} off'
            out.append(f'| {_item_link(item)} | {item["item_number"]} | {item["receipt_date"]} | '
                       f'${item["unit_price"]:.2f} | {sale} | ${m["savings"]:.2f} | {_match_cell(m)} | '
                       f'{_source_link(deal)} |')
        total = sum(m["savings"] for m in adjustable)
        out += ["", f"**💰 Potential Savings: ${total:.2f}**", "",
                "💡 Request price adjustment at the membership counter within 30 days of purchase. "
                "Item # matches are certain; check that a Name match is the product you bought before you go."]
    else:
        out.append("No current deals match items on these receipts.")

    tpd_items = sorted((i for i in items if i["tpd"]), key=lambda i: i["receipt_date"], reverse=True)
    out += ["", "## ✅ Already Applied (TPD on Receipt)", ""]
    if tpd_items:
        further = {m["item"]["id"]: m for m in matches if m["item"]["tpd"]}
        out += ["| Item | Item # | Date | Original | Paid (TPD) | TPD Savings | Source |",
                "|---|---|---|---|---|---|---|"]
        saved = 0.0
        for i in tpd_items:
            orig = _num(i["original_price"])
            tpd_savings = round(orig - i["unit_price"], 2) if orig else 0.0
            saved += tpd_savings
            m = further.get(i["id"])
            src = _source_link(m["deal"]) if m else ""
            out.append(f'| {_item_link(i)} | {i["item_number"]} | {i["receipt_date"]} | '
                       f'{f"${orig:.2f}" if orig else ""} | ${i["unit_price"]:.2f} | ${tpd_savings:.2f} | {src} |')
        out += ["", f"**🎉 Already Saved: ${saved:.2f}**", "",
                "ℹ️ These items already had a Temporary Price Drop (TPD) applied at checkout."]
    else:
        out.append("No items on these receipts had a TPD.")
    return "\n".join(out)


def _analyze(receipt_ids=None, date_from=None, date_to=None, sources=None) -> str:
    items = _receipt_items(receipt_ids)
    if not items:
        return "No receipts found. Please upload a receipt first."
    deals = _filter_deals(db.get_all_price_drops(), date_from, date_to, sources)
    if not deals:
        return "No price drops found. Please run the price scanner first."

    by_number, by_name = _candidates(items, deals)
    matches = by_number + _judge(items, by_name)
    return _report(matches, items)


def run_analysis(receipt_ids: list = None) -> str:
    """Run analysis for specific receipts or all receipts."""
    return _analyze(receipt_ids)


def run_analysis_stream(receipt_ids=None, date_from=None, date_to=None, sources=None):
    """Generator of SSE events: a status line, then the finished report (or an error)."""
    def event(payload):
        return f"data: {json.dumps(payload)}\n\n"

    yield event({"type": "status", "text": "🔄 Matching receipt items to deals with Claude Haiku 5.5..."})
    try:
        text = _analyze(receipt_ids, date_from, date_to, sources)
    except receipt_parser.ParserUnavailable as e:
        yield event({"type": "error", "text": str(e)})
        return
    except Exception as e:
        log.exception("Analysis failed")
        yield event({"type": "error", "text": f"Analysis failed: {e}"})
        return
    yield event({"type": "done", "text": text})

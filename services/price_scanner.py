import requests
from bs4 import BeautifulSoup
import re
import random
import time
import html
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from services import db

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

_MONEY = r'\$\s?(\d[\d,]*(?:\.\d{1,2})?)'
# "$4 off" / "Save $4" is a discount amount, not a price. "$50 off $500+" is a spend threshold, not an item deal.
_DISCOUNT = re.compile(rf'(?:save\s+{_MONEY}|{_MONEY}\s*off\b)', re.IGNORECASE)
_SPEND_THRESHOLD = re.compile(rf'{_MONEY}\s*off\s+(?:\$|orders?|purchases?|your)', re.IGNORECASE)
_REGULAR = re.compile(rf'(?:reg\.?|regular(?:ly)?|was|orig\.?|originally)\s*(?:price\s*)?:?\s*{_MONEY}', re.IGNORECASE)
_ITEM_NUMBER = re.compile(r'\bItem\s*(?:#|No\.?|number)?\s*:?\s*(\d{4,7})\b', re.IGNORECASE)


def _money(s: str) -> str:
    return s.replace(",", "")


def _prices(text: str) -> tuple:
    """Pull (sale_price, discount, original_price) out of deal text; any may be "".

    "$21.99 – $5 off = $16.99" -> ("16.99", "5", "21.99"); "Coffee $4 off" -> ("", "4", "");
    "Chips $9.99 (reg $12.99)" -> ("9.99", "", "12.99"). When a discount comes with a single
    price it is ambiguous whether that price is before or after the discount, so it is dropped
    and only the discount is kept.
    """
    if _SPEND_THRESHOLD.search(text):
        return "", "", ""
    discount = original = ""
    m = _DISCOUNT.search(text)
    if m:
        discount = _money(m.group(1) or m.group(2))
        text = text[:m.start()] + " " + text[m.end():]
    m = _REGULAR.search(text)
    if m:
        original = _money(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]
    prices = [_money(p) for p in re.findall(_MONEY, text)]
    if discount:
        if len(prices) >= 2 and abs(float(prices[0]) - float(discount) - float(prices[-1])) < 0.02:
            return prices[-1], discount, prices[0]
        if original and prices and abs(float(original) - float(discount) - float(prices[0])) < 0.02:
            return prices[0], discount, original
        return "", discount, original
    if not prices:
        return "", "", ""
    sale = prices[0]
    if not original and len(prices) > 1 and float(prices[1]) > float(sale):
        original = prices[1]
    return sale, "", original


def _deal(item_name, source, link, sale_price="", discount="", original_price="",
          promo_start="", promo_end="", item_number="") -> dict:
    return {
        "item_name": item_name, "sale_price": sale_price, "discount": discount,
        "original_price": original_price, "promo_start": promo_start, "promo_end": promo_end,
        "source": source, "link": link, "item_number": item_number,
    }


_REDDIT_QUERY = '$ OR sale OR clearance OR markdown OR "price drop" OR "instant savings"'


def _scrape_reddit(subreddit: str = "Costco+CostcoDeals", query: str = _REDDIT_QUERY) -> list:
    """Search r/Costco and r/CostcoDeals for last month's posts whose titles carry a price or discount.

    One RSS request: Reddit answers the .json API with 403 for unauthenticated clients, and rate
    limits RSS hard enough that one search per subreddit and keyword gets 429s.
    """
    deals = []
    atom = "{http://www.w3.org/2005/Atom}"
    for attempt in range(2):
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/search.rss",
            params={"q": query, "restrict_sr": "on", "sort": "new", "t": "month", "limit": 25},
            headers={"User-Agent": "CostcoScanner/1.0"},
            timeout=15,
        )
        if resp.status_code != 429 or attempt:
            break
        time.sleep(min(int(resp.headers.get("Retry-After", "5")), 5))  # unauthenticated RSS is rate limited
    resp.raise_for_status()
    for entry in ET.fromstring(resp.content).iter(f"{atom}entry"):
        title = html.unescape(entry.findtext(f"{atom}title") or "")
        link_el = entry.find(f"{atom}link")
        link = link_el.get("href", "") if link_el is not None else ""
        posted = (entry.findtext(f"{atom}published") or entry.findtext(f"{atom}updated") or "")[:10]
        category = entry.find(f"{atom}category")
        sub = category.get("term", "Costco") if category is not None else "Costco"

        if any(skip in title.lower() for skip in ["megathread", "thread", "how costco gets you"]):
            continue
        if "$" not in title:
            continue
        sale, discount, orig = _prices(title)
        if not (sale or discount):
            continue
        name_part = title.split("$")[0].strip().rstrip(" -–|:(")
        name_part = re.sub(r'^(Found|Spotted|Deal|Sale|Price|Clearance|PSA):\s*', '', name_part, flags=re.IGNORECASE).strip()
        name_part = re.sub(r'\b(save|for|only|now|just|at)$', '', name_part, flags=re.IGNORECASE).strip(" -–|:,")
        if 5 < len(name_part) < 80:
            deals.append(_deal(name_part, f"reddit.com/r/{sub}", link, sale, discount, orig,
                               promo_start=posted))
    return deals


def _scrape_costco_warehouse_savings() -> list:
    """Costco's own Warehouse Savings page: item numbers, final price or discount, and the valid dates.

    This is the authoritative source for the instant savings Costco honours as price adjustments.
    """
    deals = []
    resp = requests.get(
        "https://www.costco.com/warehouse-savings.html",
        headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=20,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    promo_start = promo_end = ""
    m = re.search(r'Valid\s+(\d{1,2}/\d{1,2}/\d{2,4})\s*[-–]\s*(\d{1,2}/\d{1,2}/\d{2,4})', page_text)
    if m:
        promo_start, promo_end = (_parse_us_date(d) for d in m.groups())

    for card in soup.select('div[class*="MuiGrid-grid-md-3"]'):
        text = card.get_text(" | ", strip=True)
        # Prices render as separate tokens ("$ | 1 | , | 499 | . | 99"); join them back up.
        text = re.sub(r'\$((?:\s*\|\s*[\d,.]+)+)', lambda x: "$" + re.sub(r'[|\s]', '', x.group(1)), text)
        nums = re.search(r'Item\s+([\d,\s]+\d)', text)
        if not nums:
            continue
        if "Online Only" in text:
            continue  # not sold in the warehouse, so it can't be adjusted against a warehouse receipt
        if re.search(rf'Save\s*\|?\s*{_MONEY}\s*[-–]\s*\|?\s*\$', text):
            continue  # "Save $25 - $1,600": a range over many products, no usable amount

        sale = discount = ""
        m = re.search(rf'{_MONEY}\s*\|?\s*After\s+{_MONEY}\s*OFF', text, re.IGNORECASE)
        if m:
            sale, discount = _money(m.group(1)), _money(m.group(2))
        else:
            m = re.search(rf'Save\s*\|?\s*{_MONEY}', text, re.IGNORECASE)
            if m:
                discount = _money(m.group(1))
            else:
                m = re.search(_MONEY, text)
                if not m:
                    continue
                sale = _money(m.group(1))

        a = card.find("a", href=True)
        name = (a.get_text(" ", strip=True) if a else "") or text.split("|")[0].strip()
        size = ""
        size_m = re.search(r'\|\s*([^|]{1,40}?\b(?:oz|lb|ct|pk|count|pack|fl oz|L|ml|g)\.?)\s*\|\s*Item', text)
        if size_m and size_m.group(1).strip() not in name:
            size = size_m.group(1).strip()
        full_name = f"{name}, {size}" if size else name
        item_numbers = ",".join(re.findall(r'\d{4,7}', nums.group(1)))
        original = f"{float(sale) + float(discount):.2f}" if sale and discount else ""
        deals.append(_deal(full_name[:100], "costco.com", a["href"] if a else resp.url, sale, discount, original,
                           promo_start, promo_end, item_numbers))
    return deals


def _parse_us_date(s: str) -> str:
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _scrape_costco_insider() -> list:
    """Costco Insider's weekly coupon posts: text lines like "Peet's Coffee, 32 oz $6.50 off"."""
    deals = []
    resp = requests.get("https://www.costcoinsider.com/feed/",
                        headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=15)
    resp.raise_for_status()
    content_tag = "{http://purl.org/rss/1.0/modules/content/}encoded"
    cutoff = datetime.now().astimezone() - timedelta(days=35)
    for item in ET.fromstring(resp.content).iter("item"):
        title = item.findtext("title") or ""
        if not re.search(r'coupon|insider deals|hot buys', title, re.IGNORECASE):
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if published < cutoff:
            continue
        body = BeautifulSoup(item.findtext(content_tag) or item.findtext("description") or "", "html.parser")
        text = body.get_text("\n", strip=True)

        promo_start, promo_end = _insider_dates(text, published)
        link = item.findtext("link") or "https://www.costcoinsider.com/"
        for line in text.split("\n"):
            if "$" not in line or len(line) > 160 or line.count("$") > 3:
                continue
            sale, discount, orig = _prices(line)
            if not (sale or discount):
                continue
            name = line.split("$")[0].strip(" -–:,")
            # Prose sentences ("Costco's Jumbo Pumpkins are always a great deal at") are not item lines.
            if not (5 < len(name) < 100) or re.search(r'\b(is|are|while|you|if|at)$', name, re.IGNORECASE):
                continue
            deals.append(_deal(name, "costcoinsider.com", link, sale, discount, orig, promo_start, promo_end))
    return deals


def _insider_dates(text: str, published: datetime) -> tuple:
    """"run from October 5th through October 11th" -> ISO dates; falls back to a week from publication."""
    m = re.search(r'from\s+([A-Z][a-z]+\.?\s+\d{1,2})(?:st|nd|rd|th)?,?\s+(?:through|to|until|[-–])\s+'
                  r'([A-Z][a-z]+\.?\s+\d{1,2})(?:st|nd|rd|th)?', text)
    if m:
        try:
            start = datetime.strptime(f"{m.group(1).rstrip('.')} {published.year}", "%B %d %Y")
            end = datetime.strptime(f"{m.group(2).rstrip('.')} {published.year}", "%B %d %Y")
            if start.month < published.month - 6:
                start = start.replace(year=start.year + 1)
            if end < start:
                end = end.replace(year=end.year + 1)
            return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return published.strftime("%Y-%m-%d"), (published + timedelta(days=7)).strftime("%Y-%m-%d")


def _scrape_kcl_deals() -> list:
    """Scrape The Krazy Coupon Lady Costco deals listing page."""
    deals = []
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        resp = requests.get(
            "https://thekrazycouponlady.com/coupons-for/costco",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Find deal links - they contain dates and prices in URL slugs
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            # Match deal article URLs (e.g. /2026/03/26/product-name-usd37-99-at-costco)
            date_m = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', href)
            if not date_m:
                continue

            title = ""
            # Get title from heading inside the link, or from the link text
            h = a.find(["h2", "h3", "h4"])
            if h:
                title = h.get_text(strip=True)
            elif len(a.get_text(strip=True)) > 20:
                title = a.get_text(strip=True)

            if not title or "$" not in title:
                continue

            # Skip non-deal articles
            if any(skip in title.lower() for skip in ["coupon book", "membership", "return policy", "best time"]):
                continue
            # "$2.50 back with Fetch" is an app rebate, not a price Costco will adjust to
            if re.search(r'\bback\b|fetch|rewards|rebate', title, re.IGNORECASE):
                continue

            sale, discount, orig = _prices(title)
            if not (sale or discount):
                continue

            # Extract product name (before the first price mention)
            name_part = re.split(r'(?:,\s*(?:Only|Now|Just|Save))?\s*\$', title)[0].strip()
            name_part = re.sub(r'^New\s+(?:at\s+)?Costco[:\s]*', '', name_part, flags=re.IGNORECASE).strip()
            name_part = name_part.rstrip(" -–|:,")

            if 5 < len(name_part) < 100:
                link = href
                if link.startswith("/"):
                    link = "https://thekrazycouponlady.com" + link

                deals.append(_deal(name_part[:100], "thekrazycouponlady.com", link, sale, discount, orig,
                                   promo_start="-".join(date_m.groups())))
    except Exception as e:
        print(f"KCL deals failed: {e}")
    return deals


def _scrape_kcl_coupon_book() -> list:
    """The Krazy Coupon Lady's coupon book picks: deal price, regular price and expiry per card.

    Read from the page text, not the JSON-LD: the JSON-LD offer "price" is the regular price.
    """
    deals = []
    url = "https://thekrazycouponlady.com/tips/couponing/costco-coupon-book"
    resp = requests.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=15)
    resp.raise_for_status()
    text = BeautifulSoup(resp.text, "html.parser").get_text("\n", strip=True)
    # Card: "Deal / at / Costco / <name> / $ / 19.99 / $ / 26.79 / 25 / % off ... Exp 10/18/26"
    card = re.compile(r'Deal\nat\nCostco\n(?P<name>[^\n$]{5,120})\n\$\n(?P<sale>[\d,]+(?:\.\d{2})?)\n'
                      r'(?:\$\n(?P<reg>[\d,]+(?:\.\d{2})?)\n)?(?P<rest>.{0,600}?)Exp\s+(?P<exp>\d{1,2}/\d{1,2}/\d{2,4})',
                      re.DOTALL)
    for m in card.finditer(text):
        sale, reg = _money(m["sale"]), _money(m["reg"] or "")
        discount = f"{float(reg) - float(sale):.2f}" if reg and float(reg) > float(sale) else ""
        deals.append(_deal(m["name"].strip(), "costco-coupon-book", url, sale, discount, reg,
                           promo_end=_parse_us_date(m["exp"])))
    return deals


# CostcoFan is mostly product reviews and buying guides; only titles that announce a sale are deals.
_COSTCOFAN_DEAL_TITLE = re.compile(
    r'\b(on sale|sale price|price drop|price cut|marked down|markdown|clearance|instant savings|'
    r'coupon|deal alert|now only)\b|\$\d[\d,.]*\s*off\b', re.IGNORECASE)


def _scrape_costcofan() -> list:
    """CostcoFan.com posts from the last three weeks whose titles announce a sale.

    The price comes from the title only: article bodies are reviews that quote shelf prices and
    past sales ("goes on sale $3 off"), which were being picked up as current deals.
    """
    deals = []
    resp = requests.get(
        "https://costcofan.com/wp-json/wp/v2/posts",
        params={"per_page": 50, "after": (datetime.now() - timedelta(days=21)).strftime("%Y-%m-%dT00:00:00"),
                "_fields": "date,title,link"},
        headers={"User-Agent": random.choice(USER_AGENTS)},
        timeout=15,
    )
    resp.raise_for_status()
    for post in resp.json():
        title = html.unescape(post["title"]["rendered"])
        if not _COSTCOFAN_DEAL_TITLE.search(title):
            continue
        sale, discount, orig = _prices(title)
        if not (sale or discount):
            continue
        name = re.split(r'\s*[:(]|\s+\$|\s+[-–]\s', re.sub(r'^Costco\s+', '', title, flags=re.IGNORECASE))[0].strip()
        num = _ITEM_NUMBER.search(title)
        if 5 < len(name) < 100:
            deals.append(_deal(name, "costcofan.com", post["link"], sale, discount, orig,
                               promo_start=post["date"][:10], item_number=num.group(1) if num else ""))
    return deals


def scan_price_drops(force_refresh: bool = False) -> tuple:
    """Scan for Costco price drops from US deal sources.

    Returns:
        tuple: (saved_deals, source_results) where source_results is a list of
        dicts with keys: name, count, status, duration_s, error
    """

    if not force_refresh:
        cached_count = db.get_cached_deals_count()
        if cached_count > 0:
            print(f"Using {cached_count} cached deals from today")
            return db.get_all_price_drops(), [{
                "name": "cache",
                "count": cached_count,
                "status": "cached",
                "duration_s": 0,
                "error": None,
            }]

    print("Fresh scan from US sources...")

    all_deals = []
    source_results = []
    sources = [
        ("Costco Warehouse Savings", _scrape_costco_warehouse_savings),
        ("Costco Insider", _scrape_costco_insider),
        ("Reddit r/Costco + r/CostcoDeals", _scrape_reddit),
        ("KCL Costco Deals", _scrape_kcl_deals),
        ("KCL Coupon Book", _scrape_kcl_coupon_book),
        ("CostcoFan", _scrape_costcofan),
    ]

    for name, scraper in sources:
        t0 = time.time()
        try:
            deals = scraper()
            elapsed = round(time.time() - t0, 1)
            all_deals.extend(deals)
            source_results.append({
                "name": name,
                "count": len(deals),
                "status": "ok" if deals else "empty",
                "duration_s": elapsed,
                "error": None,
            })
            print(f"  {name}: {len(deals)} deals ({elapsed}s)")
        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            source_results.append({
                "name": name,
                "count": 0,
                "status": "error",
                "duration_s": elapsed,
                "error": str(e),
            })
            print(f"  {name}: FAILED ({elapsed}s) - {e}")
        time.sleep(1)  # Rate limit

    # Deduplicate against this scan and against deals already stored
    today = datetime.now().strftime("%Y-%m-%d")
    stored = {(d["item_name"], d.get("source", ""), d.get("promo_end", "")) for d in db.get_all_price_drops()}
    seen = set()
    saved = []
    for deal in all_deals:
        promo_end = deal.get("promo_end", "")
        if promo_end and promo_end < today:
            continue  # Skip expired deals
        key = (deal["item_name"].lower().strip(), promo_end)
        if key not in seen and (deal["item_name"], deal["source"], promo_end) not in stored:
            seen.add(key)
            saved.append(db.put_price_drop(**deal))

    print(f"Saved {len(saved)} deals (skipped {len(all_deals) - len(saved)} duplicates)")
    return saved, source_results


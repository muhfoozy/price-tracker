#!/usr/bin/env python3
"""
Price Tracker — Amazon.eg & Noon Egypt
Reads products from Google Sheets, checks current prices, and sends
Telegram alerts when a price drops to (or below) the target.

Environment variables
---------------------
  BOT_TOKEN                 Telegram bot token
  CHAT_ID                   Your Telegram chat id
  SHEET_ID                  Google Spreadsheet ID
  GOOGLE_CREDENTIALS        Service-account JSON *content*   (GitHub Actions)
  GOOGLE_CREDENTIALS_FILE   Path to the service-account JSON (local runs)
  USE_BROWSER_FALLBACK      "1" (default) to allow Playwright as last resort
  PW_CHANNEL                "chrome" to use the system Google Chrome instead of Playwright's Chromium

Usage
-----
  python scraper.py                  # normal run: check, update sheet, alert
  python scraper.py --dry-run        # check only: no sheet writes, no alerts
  python scraper.py --test <URL>     # check one URL and print the result (no sheet needed)
  python scraper.py --debug          # save HTML of failed pages into ./debug/
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

# ──────────────────────────── Config ────────────────────────────

USER_NAME = "foozy"
CURRENCY = "EGP"
TZ = ZoneInfo("Africa/Cairo")
SHEET_NAME = "Products"
CHANGE_THRESHOLD = 10  # EGP — notify on any move (up or down) of at least this much

HEADERS = [
    "ID", "Name", "Store", "URL", "Target Price", "Current Price",
    "Lowest Price", "Last Checked", "Status", "Last Alert Price", "Added At",
]
COL = {h: i for i, h in enumerate(HEADERS)}  # 0-based

# TLS/HTTP2 fingerprints to rotate (curl_cffi sets a matching User-Agent + headers)
# Tested against amazon.eg: chrome124 & safari17_0 get pages without a price → removed
IMPERSONATE = ["chrome", "chrome120", "chrome119", "edge101"]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9,ar;q=0.7",
    "en-US,en;q=0.8,ar-EG;q=0.6",
    "en,ar;q=0.9",
]

BROWSER_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]

WARMUP_URLS = {
    "amazon": "https://www.amazon.eg/-/en/",
    "noon": "https://www.noon.com/egypt-en/",
}

DEBUG_DIR: Path | None = None

# Windows console defaults to a legacy code page → force UTF-8 so emoji/arrows don't crash logging
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tracker")


# ──────────────────────────── Models ────────────────────────────

@dataclass
class Result:
    price: float | None = None
    title: str | None = None
    url: str | None = None          # canonical URL (e.g. resolved short link)
    status: str = "OK"              # OK | OUT_OF_STOCK | BLOCKED | NOT_FOUND | ERROR
    source: str = ""                # which method produced the result
    error: str | None = None


@dataclass
class Product:
    id: int
    name: str
    store: str
    url: str
    target: float
    current: float | None
    lowest: float | None
    last_alert: float | None


# ──────────────────────────── Utils ─────────────────────────────

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def parse_price(value) -> float | None:
    """'EGP 1,299.00' / '١٬٢٩٩' / 1299 → 1299.0"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = str(value).translate(_AR_DIGITS).replace("\xa0", " ")
    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


def human_pause(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


def backoff(attempt: int) -> None:
    time.sleep(min(30, 4 * attempt) + random.uniform(0.5, 3))


def dump_debug(tag: str, html: str) -> None:
    if not DEBUG_DIR or not html:
        return
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(TZ):%H%M%S}_{re.sub(r'[^a-z0-9_-]+', '_', tag.lower())}.html"
    (DEBUG_DIR / name).write_text(html, encoding="utf-8", errors="ignore")
    log.info("   debug HTML saved → %s", DEBUG_DIR / name)


def fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return (f"{v:,.2f}" if v % 1 else f"{v:,.0f}") + f" {CURRENCY}"


def iter_nodes(obj):
    """Walk any JSON structure, yielding every dict."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_nodes(v)


def parse_jsonld(soup: BeautifulSoup) -> tuple[str | None, float | None, bool | None]:
    """Extract (name, price, in_stock) from schema.org Product JSON-LD."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in iter_nodes(data):
            if "Product" not in str(node.get("@type", "")):
                continue
            offers = node.get("offers")
            offers = offers if isinstance(offers, list) else [offers]
            price, in_stock = None, None
            for off in offers:
                if not isinstance(off, dict):
                    continue
                p = parse_price(off.get("price") or off.get("lowPrice"))
                if p and (price is None or p < price):
                    price = p
                avail = str(off.get("availability", ""))
                if avail:
                    in_stock = "InStock" in avail or bool(in_stock)
            return node.get("name"), price, in_stock
    return None, None, None


# ───────────────────────── HTTP client ──────────────────────────

class HttpClient:
    """One curl_cffi session per store; rotated on blocks."""

    def __init__(self) -> None:
        self._sessions: dict[str, tuple[cffi.Session, str]] = {}

    def session(self, key: str) -> cffi.Session:
        if key not in self._sessions:
            imp = os.getenv("FORCE_FP") or random.choice(IMPERSONATE)  # FORCE_FP: test one fingerprint
            s = cffi.Session(impersonate=imp)
            s.headers.update({
                "Accept-Language": random.choice(ACCEPT_LANGUAGES),
                "DNT": random.choice(["1", "0"]),
            })
            self._sessions[key] = (s, imp)
            log.debug("new %s session (%s)", key, imp)
            warm = WARMUP_URLS.get(key)
            if warm:  # collect cookies like a real visitor
                try:
                    s.get(warm, timeout=25)
                    human_pause(1.5, 3.5)
                except Exception:
                    pass
        return self._sessions[key][0]

    def fingerprint(self, key: str) -> str:
        return self._sessions.get(key, (None, "?"))[1]

    def rotate(self, key: str) -> None:
        pair = self._sessions.pop(key, None)
        if pair:
            try:
                pair[0].close()
            except Exception:
                pass


# ───────────────────── Browser (last resort) ────────────────────

class Browser:
    """Lazy Playwright Chromium — only launched if HTTP methods fail."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._pw = None
        self._browser = None

    def _start(self) -> None:
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            channel=os.getenv("PW_CHANNEL") or None,  # "chrome" = use installed Google Chrome (GitHub runners)
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )

    def get_html(self, url: str) -> str | None:
        if not self.enabled:
            return None
        ctx = None
        try:
            if not self._browser:
                self._start()
            ctx = self._browser.new_context(
                user_agent=random.choice(BROWSER_UAS),
                locale="en-US",
                timezone_id="Africa/Cairo",
                viewport={"width": random.choice([1366, 1440, 1536, 1920]),
                          "height": random.choice([768, 864, 900, 1080])},
            )
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in ("image", "media", "font")
                       else route.continue_())
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            return page.content()
        except Exception as e:
            log.warning("   browser failed: %s", e)
            return None
        finally:
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


# ─────────────────────────── Amazon ─────────────────────────────

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN)/([A-Z0-9]{10})", re.I)

AMAZON_PRICE_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div .a-price.priceToPay .a-offscreen",
    "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#apex_desktop .a-price:not(.a-text-price) .a-offscreen",
    "#apex_offerDisplay_desktop .a-price:not(.a-text-price) .a-offscreen",
    "#tp_price_block_total_price_ww .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "input#twister-plus-price-data-price",
    "input#attach-base-product-price",
]

AMAZON_OOS_WORDS = ("currently unavailable", "out of stock", "غير متوفر", "غير متاح")


def amazon_canonical(url: str) -> str | None:
    m = ASIN_RE.search(url or "")
    return f"https://www.amazon.eg/dp/{m.group(1).upper()}" if m else None


def amazon_blocked(html: str) -> bool:
    return any(k in html for k in (
        "validateCaptcha", "Type the characters you see",
        "api-services-support@amazon.com", "To discuss automated access",
    ))


def parse_amazon(html: str) -> Result:
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one("#productTitle")
    title = el.get_text(" ", strip=True) if el else None

    price = None
    for sel in AMAZON_PRICE_SELECTORS:
        el = soup.select_one(sel)
        if not el:
            continue
        price = parse_price(el.get("value") if el.name == "input" else el.get_text(strip=True))
        if price:
            break

    if not price:  # split layout: <span class="a-price-whole">849.</span><span class="a-price-fraction">00</span>
        box = soup.select_one("#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, #apex_desktop")
        whole = box.select_one(".a-price-whole") if box else None
        if whole:
            frac = box.select_one(".a-price-fraction")
            price = parse_price(whole.get_text(strip=True).rstrip(".") +
                                ("." + frac.get_text(strip=True) if frac else ""))

    if not price:
        _, price, _ = parse_jsonld(soup)

    avail = soup.select_one("#availability")
    avail_txt = avail.get_text(" ", strip=True).lower() if avail else ""
    oos = any(w in avail_txt for w in AMAZON_OOS_WORDS)
    if not price and soup.select_one("#outOfStock, #outOfStockBuyBox_feature_div"):
        oos = True

    if oos:
        return Result(title=title, status="OUT_OF_STOCK")
    if price:
        return Result(price=price, title=title, status="OK")
    return Result(title=title, status="ERROR", error="price not found in page")


def check_amazon(url: str, http: HttpClient, browser: Browser) -> Result:
    canonical = amazon_canonical(url)

    # Resolve share links (amzn.eu/d/..., a.co, amzn.to)
    if not canonical:
        try:
            r = http.session("amazon").get(url, timeout=25, allow_redirects=True)
            canonical = amazon_canonical(str(r.url)) or amazon_canonical(r.text[:5000])
        except Exception as e:
            return Result(status="ERROR", error=f"short link resolve failed: {e}")
        if not canonical:
            return Result(status="NOT_FOUND", error="could not extract ASIN")
        log.info("   resolved → %s", canonical)

    page_url = canonical.replace("/dp/", "/-/en/dp/")  # force English page
    last_err, blocked = None, False

    for attempt in range(1, 4):
        s = http.session("amazon")
        try:
            r = s.get(page_url, timeout=30, headers={"Referer": "https://www.amazon.eg/"})
        except Exception as e:
            last_err = f"network: {e}"
            log.info("   attempt %d: %s", attempt, last_err)
            http.rotate("amazon")
            backoff(attempt)
            continue

        body = r.text or ""
        if r.status_code == 404:
            return Result(url=canonical, status="NOT_FOUND", error="HTTP 404")
        if r.status_code in (429, 503) or amazon_blocked(body):
            blocked, last_err = True, f"blocked (HTTP {r.status_code}, fp={http.fingerprint('amazon')})"
            log.info("   attempt %d: %s", attempt, last_err)
            dump_debug(f"amazon_blocked_{attempt}", body)
            http.rotate("amazon")
            backoff(attempt)
            continue

        res = parse_amazon(body)
        res.url, res.source = canonical, f"http/{http.fingerprint('amazon')}"
        if res.status in ("OK", "OUT_OF_STOCK"):
            return res
        last_err = f"{res.error} (HTTP {r.status_code}, {len(body)} bytes, fp={http.fingerprint('amazon')})"
        log.info("   attempt %d: %s", attempt, last_err)
        dump_debug(f"amazon_noprice_{attempt}", body)
        http.rotate("amazon")
        backoff(attempt)

    # Last resort: real browser
    if browser.enabled:
        log.info("   trying browser fallback…")
        body = browser.get_html(page_url)
        if body and not amazon_blocked(body):
            res = parse_amazon(body)
            res.url, res.source = canonical, "browser"
            if res.status in ("OK", "OUT_OF_STOCK"):
                return res
        elif body:
            blocked = True
            dump_debug("amazon_browser_blocked", body)

    return Result(url=canonical, status="BLOCKED" if blocked else "ERROR", error=last_err)


# ──────────────────────────── Noon ──────────────────────────────

# Both styles: /egypt-en/<slug>/<SKU>/p/  (website)  and  /en-eg/<SKU>/p/  (app share links)
NOON_RE = re.compile(r"noon\.com/(egypt-en|egypt-ar|en-eg|ar-eg)/(.+?/p)(?=/|\?|#|$)", re.I)
NOON_LOCALE_EN = {"egypt-ar": "egypt-en", "ar-eg": "en-eg"}

NOON_PRICE_SELECTORS = [
    '[data-qa="div-price-now"]',
    '[data-qa*="price-now"]',
    '[class*="priceNow"]',
    '[class*="PriceNow"]',
    '[class*="sellingPrice"]',
]


def _noon_pick_offer_price(variants: list, sku: str | None) -> float | None:
    def offer_prices(v):
        out = []
        for o in v.get("offers") or []:
            p = parse_price(o.get("sale_price")) or parse_price(o.get("price"))
            if p:
                out.append(p)
        return out

    # Prefer the variant whose SKU matches the URL, else the cheapest overall
    if sku:
        for v in variants:
            if str(v.get("sku", "")).upper().startswith(sku.upper()):
                prices = offer_prices(v)
                if prices:
                    return min(prices)
    all_prices = [p for v in variants for p in offer_prices(v)]
    return min(all_prices) if all_prices else None


def parse_noon_api(data: dict, sku: str | None) -> Result | None:
    prod = data.get("product") if isinstance(data, dict) else None
    if not isinstance(prod, dict):
        return None
    title = prod.get("product_title") or prod.get("name")
    variants = prod.get("variants")
    if not isinstance(variants, list):
        return None  # unexpected shape → let fallbacks handle it
    price = _noon_pick_offer_price(variants, sku)
    if price:
        return Result(price=price, title=title, status="OK", source="noon-api")
    return Result(title=title, status="OUT_OF_STOCK", source="noon-api")


def parse_noon_html(html: str) -> Result | None:
    soup = BeautifulSoup(html, "lxml")
    name, price, in_stock = parse_jsonld(soup)

    if not name:
        og = soup.select_one('meta[property="og:title"]')
        h1 = soup.select_one("h1")
        name = (og.get("content") if og else None) or (h1.get_text(" ", strip=True) if h1 else None)

    if not price:  # Next.js embedded state
        nd = soup.select_one("script#__NEXT_DATA__")
        if nd:
            try:
                for node in iter_nodes(json.loads(nd.string or "")):
                    p = parse_price(node.get("sale_price")) or parse_price(node.get("salePrice"))
                    if p:
                        price = p
                        break
            except (json.JSONDecodeError, TypeError):
                pass

    if not price:  # rendered DOM
        for sel in NOON_PRICE_SELECTORS:
            el = soup.select_one(sel)
            if el:
                price = parse_price(el.get_text(" ", strip=True))
                if price:
                    break

    if price:
        return Result(price=price, title=name, status="OK")
    if in_stock is False:
        return Result(title=name, status="OUT_OF_STOCK")
    return None


def check_noon(url: str, http: HttpClient, browser: Browser) -> Result:
    m = NOON_RE.search(url)
    if not m:
        return Result(status="NOT_FOUND", error="unrecognized Noon URL")
    loc = m.group(1).lower()
    loc = NOON_LOCALE_EN.get(loc, loc)
    path = m.group(2)                                 # "<slug>/<SKU>/p"  or  "<SKU>/p"
    canonical = f"https://www.noon.com/{loc}/{path}/"
    parts = path.split("/")
    sku = parts[-2] if len(parts) >= 2 else None
    last_err = None

    # 1) Internal catalog API (fast, JSON). Undocumented → may change; fallbacks below.
    api_url = f"https://www.noon.com/_svc/catalog/api/v3/u/{path}/"
    for attempt in range(1, 3):
        s = http.session("noon")
        try:
            r = s.get(api_url, timeout=30, headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": canonical,
                "x-locale": "en-eg",
                "x-mp": "noon",
                "x-platform": "web",
                "x-content": "desktop",
            })
            if 200 <= r.status_code < 300 and "json" in r.headers.get("content-type", ""):
                res = parse_noon_api(r.json(), sku)
                if res:
                    res.url = canonical
                    return res
                last_err = "api: unexpected JSON shape"
                break
            last_err = f"api HTTP {r.status_code}"
            if r.status_code == 404:
                break
        except Exception as e:
            last_err = f"api network: {e}"
        http.rotate("noon")
        backoff(attempt)

    # 2) Product page HTML (JSON-LD / __NEXT_DATA__)
    try:
        r = http.session("noon").get(canonical, timeout=30,
                                     headers={"Referer": "https://www.noon.com/egypt-en/"})
        if r.status_code == 404:
            return Result(url=canonical, status="NOT_FOUND", error="HTTP 404")
        if 200 <= r.status_code < 300:
            res = parse_noon_html(r.text)
            if res:
                res.url, res.source = canonical, "noon-html"
                return res
            dump_debug("noon_html_noprice", r.text)
            last_err = "html: price not found"
        else:
            last_err = f"html HTTP {r.status_code}"
    except Exception as e:
        last_err = f"html network: {e}"

    # 3) Full JS rendering
    if browser.enabled:
        log.info("   trying browser fallback…")
        body = browser.get_html(canonical)
        if body:
            res = parse_noon_html(body)
            if res:
                res.url, res.source = canonical, "browser"
                return res
            dump_debug("noon_browser_noprice", body)

    blocked = any(x in (last_err or "") for x in ("403", "429", "503"))
    return Result(url=canonical, status="BLOCKED" if blocked else "ERROR", error=last_err)


# ─────────────────────────── Dispatch ───────────────────────────

def detect_store(url: str) -> str | None:
    u = (url or "").lower()
    if "noon.com" in u:
        return "noon"
    if any(h in u for h in ("amazon.eg", "amzn.eu", "amzn.to", "a.co/")):
        return "amazon"
    return None


def check_url(url: str, http: HttpClient, browser: Browser) -> Result:
    store = detect_store(url)
    try:
        if store == "amazon":
            return check_amazon(url, http, browser)
        if store == "noon":
            return check_noon(url, http, browser)
        return Result(status="NOT_FOUND", error="unsupported store")
    except Exception as e:  # never let one product kill the run
        log.exception("   unexpected error")
        return Result(status="ERROR", error=str(e))


# ─────────────────────────── Telegram ───────────────────────────

def send_telegram(text: str) -> None:
    token, chat_id = os.environ["BOT_TOKEN"], os.environ["CHAT_ID"]
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": False, "prefer_small_media": True},
            },
            timeout=20,
        )
        if not r.ok:
            log.error("Telegram error: %s", r.text)
    except requests.RequestException as e:
        log.error("Telegram request failed: %s", e)


def _store_label(url: str) -> str:
    return "Amazon" if detect_store(url) == "amazon" else "Noon"


def _target_line(price: float, target: float) -> str:
    if price <= target:
        return f"🎯 Target: {fmt_money(target)}  ✅ <b>reached</b>"
    return f"🎯 Target: {fmt_money(target)}  ({fmt_money(price - target)} to go)"


def msg_first_check(p: Product, res: Result) -> str:
    esc = htmllib.escape
    name = res.title or p.name or f"Product #{p.id}"
    head = (f"🎯 <b>Already at your target, {USER_NAME}!</b>" if res.price <= p.target
            else "🆕 <b>Now tracking</b>")
    return "\n".join([
        head, "",
        f"📦 {esc(name[:150])}",
        f"🏪 {_store_label(p.url)} · #{p.id}",
        f"💰 Current: <b>{fmt_money(res.price)}</b>",
        _target_line(res.price, p.target),
        f"🔔 You'll be notified on every change of {CHANGE_THRESHOLD}+ {CURRENCY}.",
        "",
        f'🔗 <a href="{esc(res.url or p.url)}">Open product</a>',
    ])


def msg_price_change(p: Product, res: Result, before: float, lowest: float | None,
                     target_crossed: bool) -> str:
    esc = htmllib.escape
    name = res.title or p.name or f"Product #{p.id}"
    diff = res.price - before
    pct = f"{abs(diff) / before * 100:.1f}%" if before else ""
    if diff < 0:
        move = f"📉 <b>Price dropped −{fmt_money(abs(diff))}</b> ({pct})"
    elif diff > 0:
        move = f"📈 <b>Price went up +{fmt_money(diff)}</b> ({pct})"
    else:
        move = "➖ <b>Price unchanged</b>"
    lines = []
    if target_crossed:
        lines += [f"🎯🔥 <b>Target reached, {USER_NAME}!</b>"]
    lines += [
        move, "",
        f"📦 {esc(name[:150])}",
        f"🏪 {_store_label(p.url)} · #{p.id}",
        f"💰 Now: <b>{fmt_money(res.price)}</b>  (was {fmt_money(before)})",
        _target_line(res.price, p.target),
        f"📊 Lowest seen: {fmt_money(lowest)}",
        "",
        f'🔗 <a href="{esc(res.url or p.url)}">Open product</a>',
    ]
    return "\n".join(lines)


# ─────────────────────────── Sheets ─────────────────────────────

def open_worksheet():
    import gspread

    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE")
    if creds_json:
        gc = gspread.service_account_from_dict(json.loads(creds_json))
    elif creds_file:
        gc = gspread.service_account(filename=creds_file)
    else:
        sys.exit("❌ Set GOOGLE_CREDENTIALS (JSON content) or GOOGLE_CREDENTIALS_FILE (path).")
    return gc.open_by_key(os.environ["SHEET_ID"]).worksheet(SHEET_NAME)


def _num(v) -> float | None:
    return parse_price(v)


def read_products(ws) -> list[Product]:
    rows = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
    out = []
    for row in rows[1:]:
        row = list(row) + [""] * (len(HEADERS) - len(row))
        url = str(row[COL["URL"]]).strip()
        target = _num(row[COL["Target Price"]])
        try:
            pid = int(float(row[COL["ID"]]))
        except (TypeError, ValueError):
            continue
        if not url or not target:
            continue
        out.append(Product(
            id=pid,
            name=str(row[COL["Name"]] or ""),
            store=str(row[COL["Store"]] or ""),
            url=url,
            target=target,
            current=_num(row[COL["Current Price"]]),
            lowest=_num(row[COL["Lowest Price"]]),
            last_alert=_num(row[COL["Last Alert Price"]]),
        ))
    return out


def write_updates(ws, updates: dict[int, dict]) -> None:
    if not updates:
        return
    # Re-read IDs right before writing: rows may have been deleted via /delete meanwhile
    ids = ws.col_values(1)
    id_to_row = {str(v).strip(): i + 1 for i, v in enumerate(ids) if i > 0 and str(v).strip()}

    values, texts = [], []
    for pid, u in updates.items():
        r = id_to_row.get(str(pid))
        if not r:
            log.info("#%s was deleted during the run — skipping write", pid)
            continue
        values.append({"range": f"F{r}:J{r}", "values": [[
            u["current"], u["lowest"], u["checked"], u["status"], u["last_alert"],
        ]]})
        if u.get("name"):
            texts.append({"range": f"B{r}", "values": [[u["name"]]]})
        if u.get("url"):
            texts.append({"range": f"D{r}", "values": [[u["url"]]]})

    if values:
        ws.batch_update(values, value_input_option="USER_ENTERED")  # dates/numbers parsed
    if texts:
        ws.batch_update(texts, value_input_option="RAW")            # names/URLs never become formulas


# ──────────────────────────── Main ──────────────────────────────

def run(dry_run: bool) -> int:
    for var in ("SHEET_ID",) + (() if dry_run else ("BOT_TOKEN", "CHAT_ID")):
        if not os.getenv(var):
            sys.exit(f"❌ Missing environment variable: {var}")

    ws = open_worksheet()
    products = read_products(ws)
    log.info("Loaded %d product(s) from sheet", len(products))
    if not products:
        return 0

    random.shuffle(products)  # no predictable request order
    http = HttpClient()
    browser = Browser(enabled=os.getenv("USE_BROWSER_FALLBACK", "1") == "1")
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    updates: dict[int, dict] = {}
    stats: Counter = Counter()

    try:
        for i, p in enumerate(products):
            if i:
                human_pause(4, 9)
            log.info("#%s  %s", p.id, p.url)
            res = check_url(p.url, http, browser)
            stats[res.status] += 1
            log.info("   → %s | %s | %s%s", res.status, fmt_money(res.price), res.source or "-",
                     f" | {res.error}" if res.error else "")

            price = res.price if res.status == "OK" else None
            current = price if price is not None else p.current
            lowest = min(x for x in (p.lowest, price) if x is not None) if (p.lowest or price) else None
            baseline = p.last_alert  # price at the last notification ("Last Alert Price" column)
            msg = None

            if price is not None:
                if baseline is None:
                    # New product (or target changed via /add) → report the starting price
                    msg = msg_first_check(p, res)
                    stats["first_check"] += 1
                else:
                    was_hit = p.current is not None and p.current <= p.target
                    crossed = price <= p.target and not was_hit
                    if crossed or abs(price - baseline) >= CHANGE_THRESHOLD:
                        msg = msg_price_change(p, res, baseline, lowest, crossed)
                        stats["target_hits" if crossed else "changes"] += 1
                if msg:
                    log.info("   🔔 NOTIFY: %s (baseline %s, target %s)",
                             fmt_money(price), fmt_money(baseline), fmt_money(p.target))
                    if not dry_run:
                        send_telegram(msg)
                    baseline = price
            last_alert = baseline

            u = {
                "current": current if current is not None else "",
                "lowest": lowest if lowest is not None else "",
                "checked": now,
                "status": res.status,
                "last_alert": last_alert if last_alert is not None else "",
            }
            if res.title and (not p.name or p.name.startswith("(pending")):
                u["name"] = res.title[:200]
            if res.url and res.url != p.url:
                u["url"] = res.url
            updates[p.id] = u
    finally:
        browser.close()

    if dry_run:
        log.info("Dry run — sheet not updated, no alerts sent")
    else:
        write_updates(ws, updates)
        log.info("Sheet updated")

    log.info("Summary: %s", dict(stats))

    # Exit non-zero if EVERYTHING failed → GitHub marks the run as failed and emails you
    ok = stats["OK"] + stats["OUT_OF_STOCK"]
    if ok == 0 and len(products) > 0:
        log.error("All checks failed this run (possibly blocked).")
        return 1
    return 0


def main() -> None:
    global DEBUG_DIR
    ap = argparse.ArgumentParser(description="Amazon.eg / Noon Egypt price tracker")
    ap.add_argument("--dry-run", action="store_true", help="no sheet writes, no alerts")
    ap.add_argument("--test", metavar="URL", help="check a single URL and print the result")
    ap.add_argument("--debug", action="store_true", help="save HTML of failed pages to ./debug")
    args = ap.parse_args()

    if args.debug or os.getenv("DEBUG") == "1":
        DEBUG_DIR = Path("debug")
        log.setLevel(logging.DEBUG)

    if args.test:
        browser = Browser(enabled=os.getenv("USE_BROWSER_FALLBACK", "1") == "1")
        try:
            res = check_url(args.test, HttpClient(), browser)
        finally:
            browser.close()
        print(json.dumps(asdict(res), ensure_ascii=False, indent=2))
        sys.exit(0 if res.status in ("OK", "OUT_OF_STOCK") else 1)

    sys.exit(run(args.dry_run))


if __name__ == "__main__":
    main()

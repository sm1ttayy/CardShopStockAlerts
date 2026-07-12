"""TCG restock/preorder watcher for Shopify stores.

Two passes per store:
  1. Radar  - keyword search (search/suggest.json) finds new sealed-product
              listings and passively refreshes availability of known ones
              (suggest results include price + available, so this costs no
              extra requests).
  2. Flip   - products currently marked unavailable (preorder targets and
              out-of-stock items) get an individual /products/<handle>.js
              check; available: false -> true fires the money alert.
              Once available, a product leaves the fetch list until the
              radar sees it go out of stock again.

State lives in state.json. First run per store is a silent baseline so you
don't get hundreds of alerts for products that already exist.

Alerts go to the Discord webhook in DISCORD_WEBHOOK_URL; without it (or with
--dry-run) they print to stdout. Stdlib only, no dependencies.
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

for _stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tcg-restock-watcher/1.0"
TIMEOUT = 15
SSL_CTX = ssl.create_default_context()


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def to_price(value):
    """Normalize Shopify price fields.

    /products/<handle>.js returns integer cents; search/suggest.json returns
    string (or float) dollars — so the type, not the magnitude, decides.
    """
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, int):
        return value / 100.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def search_products(store_url, query):
    q = urllib.parse.quote(query)
    url = f"{store_url}/search/suggest.json?q={q}&resources[type]=product&resources[limit]=10"
    data = http_json(url)
    if not data:
        return []
    return (data.get("resources", {}).get("results", {}) or {}).get("products", []) or []


def fetch_product(store_url, handle):
    return http_json(f"{store_url}/products/{handle}.js")


class Alert:
    def __init__(self, kind, store, title, price, currency, url, detail=""):
        self.kind = kind          # NEW LISTING | PREORDER/RESTOCK LIVE | PRICE CHANGE
        self.store = store
        self.title = title
        self.price = price
        self.currency = currency
        self.url = url
        self.detail = detail

    def text(self):
        price = f"{self.price:.2f} {self.currency}" if self.price else "price TBA"
        line = f"[{self.kind}] {self.title} — {price} @ {self.store}\n{self.url}"
        if self.detail:
            line += f"\n{self.detail}"
        return line


def send_discord(webhook, alerts):
    icons = {"NEW LISTING": "🆕", "PREORDER/RESTOCK LIVE": "🟢", "PRICE CHANGE": "💲"}
    embeds = []
    for a in alerts:
        price = f"{a.price:.2f} {a.currency}" if a.price else "price TBA"
        desc = f"**{price}** at {a.store}"
        if a.detail:
            desc += f"\n{a.detail}"
        embeds.append({
            "title": f"{icons.get(a.kind, '🔔')} {a.kind}: {a.title}"[:256],
            "description": desc[:2000],
            "url": a.url,
        })
    for i in range(0, len(embeds), 10):  # Discord: max 10 embeds per message
        body = json.dumps({"embeds": embeds[i:i + 10]}).encode()
        req = urllib.request.Request(
            webhook, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX):
            pass
        time.sleep(1)


def run(dry_run=False):
    config = load_json(CONFIG_PATH, None)
    if config is None:
        sys.exit(f"cannot read {CONFIG_PATH}")
    state = load_json(STATE_PATH, {"stores": {}})
    delay = config.get("request_delay_ms", 250) / 1000.0
    alerts = []
    errors = []

    seeded = {}
    for w in config.get("watched", []):
        seeded.setdefault(w["store"], {})[w["handle"]] = w.get("note", "")

    for store in config["stores"]:
        surl, sname, cur = store["url"], store["name"], store.get("currency", "USD")
        st = state["stores"].setdefault(surl, {"products": {}, "baselined": False})
        products = st["products"]  # handle -> {title, game, available, price, note?, delisted?}
        baselined = st["baselined"]
        fresh = set()  # handles whose state was refreshed this run

        # ---- pass 1: keyword radar (discovers new, passively refreshes known) ----
        for game, rule in config["games"].items():
            inc = re.compile(rule["include"], re.I)
            exc = re.compile(rule["exclude"], re.I) if rule.get("exclude") else None
            for query in rule["queries"]:
                try:
                    results = search_products(surl, query)
                except Exception as e:
                    errors.append(f"{sname}: search '{query}' failed: {e}")
                    continue
                finally:
                    time.sleep(delay)
                for p in results:
                    title, handle = p.get("title", ""), p.get("handle", "")
                    price = to_price(p.get("price"))
                    available = bool(p.get("available"))
                    if not handle or not inc.search(title):
                        continue
                    if exc and exc.search(title):
                        continue
                    if price and price < rule.get("min_price", 0):
                        continue
                    url = f"{surl}/products/{handle}"

                    if handle in products:  # known: passive availability refresh
                        w = products[handle]
                        if w.get("available") is False and available and not w.get("delisted"):
                            alerts.append(Alert("PREORDER/RESTOCK LIVE", sname, title, price, cur,
                                                url, w.get("note", "")))
                        w.update({"title": title, "available": available, "price": price})
                        fresh.add(handle)
                        continue

                    products[handle] = {"title": title, "game": game,
                                        "available": available, "price": price}
                    fresh.add(handle)
                    if baselined:
                        status = "available NOW" if available else "listed but not yet buyable — now watching"
                        alerts.append(Alert("NEW LISTING", sname, title, price, cur, url,
                                            f"{game} · {status}"))

        # ---- pass 2: active flip checks for unavailable products ----
        for handle, note in seeded.get(surl, {}).items():
            entry = products.setdefault(handle, {"title": handle, "available": None, "price": 0.0})
            entry["note"] = note

        to_check = [h for h, w in products.items()
                    if h not in fresh and not w.get("delisted") and w.get("available") is not True]
        for handle in to_check:
            w = products[handle]
            try:
                p = fetch_product(surl, handle)
            except Exception as e:
                errors.append(f"{sname}: product '{handle}' failed: {e}")
                continue
            finally:
                time.sleep(delay)
            if p is None:  # 404 — delisted; stop checking it
                w["delisted"] = True
                continue
            title = p.get("title", handle)
            available = bool(p.get("available"))
            price = to_price(p.get("price"))
            prev_avail, prev_price = w.get("available"), w.get("price", 0.0)
            url = f"{surl}/products/{handle}"
            note = w.get("note", "")

            if prev_avail is False and available:
                alerts.append(Alert("PREORDER/RESTOCK LIVE", sname, title, price, cur, url, note))
            elif prev_avail is not None and prev_price and price and abs(price - prev_price) / prev_price > 0.01:
                alerts.append(Alert("PRICE CHANGE", sname, title, price, cur, url,
                                    f"was {prev_price:.2f} {cur}" + (f" · {note}" if note else "")))
            elif prev_avail is not None and prev_price == 0.0 and price > 0:
                alerts.append(Alert("PRICE CHANGE", sname, title, price, cur, url,
                                    "price set (was TBA)" + (f" · {note}" if note else "")))

            w.update({"title": title, "available": available, "price": price})

        if not baselined:
            n_wait = sum(1 for w in products.values() if w.get("available") is False)
            print(f"[baseline] {sname}: tracking {len(products)} products ({n_wait} awaiting availability)")
            st["baselined"] = True

    # ---- deliver ----
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if alerts:
        print(f"\n{len(alerts)} alert(s):")
        for a in alerts:
            print("  " + a.text().replace("\n", "\n  "))
        if webhook and not dry_run:
            send_discord(webhook, alerts)
            print("→ sent to Discord")
        elif not webhook:
            print("→ DISCORD_WEBHOOK_URL not set; printed only")
    else:
        print("no alerts this run")

    for e in errors:
        print(f"[warn] {e}", file=sys.stderr)

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def test_alert():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    a = Alert("PREORDER/RESTOCK LIVE", "Test Store", "One Piece OP-17 Booster Box (test alert)",
              119.76, "USD", "https://example.com", "webhook connectivity test")
    print(a.text())
    if webhook:
        send_discord(webhook, [a])
        print("→ sent to Discord")
    else:
        print("→ DISCORD_WEBHOOK_URL not set")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="never post to Discord, print alerts only")
    ap.add_argument("--test-alert", action="store_true", help="send a sample alert to verify the webhook")
    args = ap.parse_args()
    if args.test_alert:
        test_alert()
    else:
        run(dry_run=args.dry_run)

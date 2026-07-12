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


class PriceJudge:
    """Two-sided price flags, never hiding an alert: ⚠️ above the per-game
    max_price, 💎 at/below the per-game deal_price (deals only for titles
    matching deal_match, so a cheap starter deck isn't a "deal"). Thresholds
    are USD; store prices are converted via config fx_to_usd first."""

    def __init__(self, config):
        self.fx = config.get("fx_to_usd", {"USD": 1.0})
        games = config.get("games", {})
        self.caps = {g: r.get("max_price") for g, r in games.items()}
        self.deals = {g: r.get("deal_price") for g, r in games.items()}
        exempt = config.get("cap_exempt", "")
        self.exempt = re.compile(exempt, re.I) if exempt else None
        deal_match = config.get("deal_match", "booster (box|display)")
        self.deal_match = re.compile(deal_match, re.I)

    def flag(self, game, title, price, currency):
        if not price or (self.exempt and self.exempt.search(title)):
            return ""
        usd = price * self.fx.get(currency, 1.0)
        cap = self.caps.get(game)
        if cap and usd > cap:
            return f"⚠️ OVER LIMIT: ~{usd:.0f} USD vs your {cap:.0f} cap"
        deal = self.deals.get(game)
        if deal and usd <= deal and self.deal_match.search(title):
            return f"💎 DEAL: ~{usd:.0f} USD ≤ your {deal:.0f} target"
        return ""


class Alert:
    def __init__(self, kind, store, title, price, currency, url, detail="", game=""):
        self.kind = kind          # NEW LISTING | PREORDER/RESTOCK LIVE | PRICE CHANGE
        self.store = store
        self.title = title
        self.price = price
        self.currency = currency
        self.url = url
        self.detail = detail
        self.game = game          # routes to the game's webhook when configured

    def text(self):
        price = f"{self.price:.2f} {self.currency}" if self.price else "price TBA"
        line = f"[{self.kind}] {self.title} — {price} @ {self.store}\n{self.url}"
        if self.detail:
            line += f"\n{self.detail}"
        return line


def observe(products, fresh, baselined, judge, alerts, sname, cur, surl,
            game, handle, title, price, available):
    """Radar and sweep both funnel every observed product through here:
    registers new listings, detects availability flips and >10% price drops
    on known ones."""
    url = f"{surl}/products/{handle}"
    flag = judge.flag(game, title, price, cur)
    if handle in products:
        w = products[handle]
        prev_price = w.get("price", 0.0)
        if not w.get("delisted"):
            if w.get("available") is False and available:
                detail = " · ".join(x for x in (w.get("note", ""), flag) if x)
                alerts.append(Alert("PREORDER/RESTOCK LIVE", sname, title, price, cur, url,
                                    detail, game=game))
            elif available and prev_price and price and price < prev_price * 0.9:
                detail = " · ".join(x for x in (f"was {prev_price:.2f} {cur}", flag) if x)
                alerts.append(Alert("PRICE DROP", sname, title, price, cur, url,
                                    detail, game=game))
        w.update({"title": title, "available": available, "price": price})
        fresh.add(handle)
        return
    products[handle] = {"title": title, "game": game, "available": available, "price": price}
    fresh.add(handle)
    if baselined:
        status = "available NOW" if available else "listed but not yet buyable — now watching"
        detail = " · ".join(x for x in (f"{game} · {status}", flag) if x)
        alerts.append(Alert("NEW LISTING", sname, title, price, cur, url, detail, game=game))


def game_for(config, title):
    """First game whose include regex matches the title (and exclude doesn't)."""
    for game, rule in config.get("games", {}).items():
        if re.search(rule["include"], title, re.I):
            exc = rule.get("exclude")
            if exc and re.search(exc, title, re.I):
                return None
            return game
    return None


def webhook_for(config, game):
    """Game-specific webhook (config webhook_env maps game -> env var name),
    falling back to DISCORD_WEBHOOK_URL."""
    env_name = config.get("webhook_env", {}).get(game)
    if env_name:
        url = os.environ.get(env_name, "").strip()
        if url:
            return url
    return os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def send_discord(webhook, alerts):
    icons = {"NEW LISTING": "🆕", "PREORDER/RESTOCK LIVE": "🟢", "PRICE CHANGE": "💲",
             "PRICE DROP": "📉"}
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


def run(dry_run=False, do_sweep=False):
    config = load_json(CONFIG_PATH, None)
    if config is None:
        sys.exit(f"cannot read {CONFIG_PATH}")
    state = load_json(STATE_PATH, {"stores": {}})
    delay = config.get("request_delay_ms", 250) / 1000.0
    judge = PriceJudge(config)
    alerts = []
    errors = []

    seeded = {}
    for w in config.get("watched", []):
        seeded.setdefault(w["store"], {})[w["handle"]] = w

    for store in config["stores"]:
        surl, sname, cur = store["url"], store["name"], store.get("currency", "USD")
        st = state["stores"].setdefault(surl, {"products": {}, "baselined": False})
        products = st["products"]  # handle -> {title, game, available, price, note?, delisted?}
        baselined = st["baselined"]
        fresh = set()  # handles whose state was refreshed this run

        # ---- sweep mode: page the store's full catalog (search only ever
        # shows the top 10 per query; this catches everything else) ----
        if do_sweep:
            match = re.compile(config.get("sweep_match", "booster (box|display)"), re.I)
            page = 1
            while page <= 20:
                try:
                    data = http_json(f"{surl}/products.json?limit=250&page={page}")
                except Exception as e:
                    errors.append(f"{sname}: sweep page {page} failed: {e}")
                    break
                finally:
                    time.sleep(delay)
                items = (data or {}).get("products", []) or []
                for p in items:
                    title, handle = p.get("title", ""), p.get("handle", "")
                    matchable = " ".join(filter(None, [title, p.get("product_type"),
                                                       " ".join(p.get("tags") or [])]))
                    if not handle or not match.search(matchable):
                        continue
                    game = game_for(config, matchable)
                    if not game:
                        continue
                    variants = p.get("variants") or []
                    available = any(v.get("available") for v in variants)
                    price = to_price(variants[0].get("price")) if variants else 0.0
                    if price and price < config["games"][game].get("min_price", 0):
                        continue
                    observe(products, fresh, baselined, judge, alerts, sname, cur, surl,
                            game, handle, title, price, available)
                if len(items) < 250:
                    break
                page += 1

        # ---- pass 1: keyword radar (discovers new, passively refreshes
        # known; skipped in sweep mode) ----
        for game, rule in ({} if do_sweep else config["games"]).items():
            inc = re.compile(rule["include"], re.I)
            exc = re.compile(rule["exclude"], re.I) if rule.get("exclude") else None
            for query in rule["queries"]:
                # a query is a plain string, or {"q": ..., "min_price": ...}
                # when a product type needs its own price floor
                if isinstance(query, dict):
                    q_text, q_min = query["q"], query.get("min_price", rule.get("min_price", 0))
                else:
                    q_text, q_min = query, rule.get("min_price", 0)
                try:
                    results = search_products(surl, q_text)
                except Exception as e:
                    errors.append(f"{sname}: search '{q_text}' failed: {e}")
                    continue
                finally:
                    time.sleep(delay)
                for p in results:
                    title, handle = p.get("title", ""), p.get("handle", "")
                    # some stores omit the game from titles; type and tags
                    # still carry it (suggest.json calls product_type "type")
                    matchable = " ".join(filter(None, [title, p.get("type"),
                                                       " ".join(p.get("tags") or [])]))
                    price = to_price(p.get("price"))
                    available = bool(p.get("available"))
                    if not handle or not inc.search(matchable):
                        continue
                    if exc and exc.search(matchable):
                        continue
                    if price and price < q_min:
                        continue
                    observe(products, fresh, baselined, judge, alerts, sname, cur, surl,
                            game, handle, title, price, available)

        # ---- pass 2: active flip checks for unavailable products ----
        for handle, seed in seeded.get(surl, {}).items():
            entry = products.setdefault(handle, {"title": handle, "available": None, "price": 0.0})
            entry["note"] = seed.get("note", "")
            if seed.get("game"):
                entry["game"] = seed["game"]

        candidates = [h for h, w in products.items()
                      if h not in fresh and not w.get("delisted") and w.get("available") is not True]
        # seeded watches are checked every run; the rest rotate through a
        # per-store cap so the run stays bounded as the tracked list grows
        seeded_handles = set(seeded.get(surl, {}))
        must = [h for h in candidates if h in seeded_handles]
        rest = sorted(h for h in candidates if h not in seeded_handles)
        limit = config.get("max_active_checks_per_store", 40)
        cursor = st.get("check_cursor", 0)
        rotated = [rest[(cursor + i) % len(rest)] for i in range(min(limit, len(rest)))] if rest else []
        if rest:
            st["check_cursor"] = (cursor + len(rotated)) % len(rest)
        for handle in must + rotated:
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
            price_flag = judge.flag(w.get("game", ""), title, price, cur)
            note = " · ".join(x for x in (w.get("note", ""), price_flag) if x)

            game = w.get("game", "")
            if prev_avail is False and available:
                alerts.append(Alert("PREORDER/RESTOCK LIVE", sname, title, price, cur, url, note,
                                    game=game))
            elif prev_avail is not None and prev_price and price and abs(price - prev_price) / prev_price > 0.01:
                alerts.append(Alert("PRICE CHANGE", sname, title, price, cur, url,
                                    f"was {prev_price:.2f} {cur}" + (f" · {note}" if note else ""),
                                    game=game))
            elif prev_avail is not None and prev_price == 0.0 and price > 0:
                alerts.append(Alert("PRICE CHANGE", sname, title, price, cur, url,
                                    "price set (was TBA)" + (f" · {note}" if note else ""),
                                    game=game))

            w.update({"title": title, "available": available, "price": price})

        if not baselined:
            n_wait = sum(1 for w in products.values() if w.get("available") is False)
            print(f"[baseline] {sname}: tracking {len(products)} products ({n_wait} awaiting availability)")
            st["baselined"] = True

    # ---- deliver ----
    if alerts:
        print(f"\n{len(alerts)} alert(s):")
        for a in alerts:
            print("  " + a.text().replace("\n", "\n  "))
        if not dry_run:
            groups = {}  # webhook url -> [alerts]
            for a in alerts:
                hook = webhook_for(config, a.game)
                if hook:
                    groups.setdefault(hook, []).append(a)
            for hook, batch in groups.items():
                send_discord(hook, batch)
            if groups:
                print(f"→ sent to Discord ({len(groups)} channel(s))")
            else:
                print("→ no webhook configured; printed only")
    else:
        print("no alerts this run")

    for e in errors:
        print(f"[warn] {e}", file=sys.stderr)

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def test_alert():
    """Send one routing-test alert per distinct configured webhook, so you can
    see in Discord which games land in which channel."""
    config = load_json(CONFIG_PATH, {})
    by_hook = {}
    for g in list(config.get("games", {})) or ["One Piece"]:
        by_hook.setdefault(webhook_for(config, g), []).append(g)
    for hook, games in by_hook.items():
        label = ", ".join(games)
        a = Alert("PREORDER/RESTOCK LIVE", "Test Store", f"Routing test — {label}",
                  119.76, "USD", "https://example.com",
                  f"alerts for {label} arrive in this channel")
        print(a.text())
        if hook:
            send_discord(hook, [a])
            print("→ sent")
        else:
            print("→ no webhook configured for these games")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="never post to Discord, print alerts only")
    ap.add_argument("--test-alert", action="store_true", help="send a sample alert to verify the webhook")
    ap.add_argument("--sweep", action="store_true",
                    help="full-catalog sweep instead of keyword radar (run daily)")
    args = ap.parse_args()
    if args.test_alert:
        test_alert()
    else:
        run(dry_run=args.dry_run, do_sweep=args.sweep)

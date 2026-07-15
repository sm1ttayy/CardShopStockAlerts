"""TCG restock/preorder watcher for Shopify stores.

Stores are scanned in parallel (politely serial within each store), and each
store's alerts dispatch to Discord the moment that store finishes.

Per store:
  Radar (default) - keyword search (search/suggest.json) finds new
              sealed-product listings and passively refreshes known ones.
  Sweep (--sweep, daily) - pages the store's complete products.json, catching
              booster boxes the top-10 search window misses.
  Flip checks - unavailable products get an individual /products/<handle>.js
              check; available: false -> true fires the money alert. Items
              unavailable for dormant_days stop being actively checked (the
              daily sweep still passively covers them).

Alert hygiene: nothing over hard_max_usd ever alerts; restock alerts have a
cooldown so flapping inventory can't spam; prices are flagged ⚠️ over the
per-game cap and 💎 at/below the per-game deal target.

--digest posts a per-game "value board" (cheapest in-stock booster boxes)
plus a health summary; run it daily after the sweep.

State lives in state.json. First run per store is a silent baseline.
Stdlib only, no dependencies.
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
from concurrent.futures import ThreadPoolExecutor, as_completed

for _stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tcg-restock-watcher/1.0"
TIMEOUT = 15
SSL_CTX = ssl.create_default_context()


def http_json(url, _retried=False):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if not _retried and e.code in (429, 430, 500, 502, 503):
            time.sleep(2)
            return http_json(url, _retried=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        if not _retried:  # transient network hiccups get one retry
            time.sleep(2)
            return http_json(url, _retried=True)
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
    """Price policy: suppress() hides anything over hard_max_usd entirely;
    flag() marks ⚠️ above the per-game max_price and 💎 at/below the per-game
    deal_price (deals only for titles matching deal_match). Thresholds are
    USD; store prices are converted via config fx_to_usd first."""

    def __init__(self, config):
        self.fx = config.get("fx_to_usd", {"USD": 1.0})
        games = config.get("games", {})
        self.caps = {g: r.get("max_price") for g, r in games.items()}
        self.deals = {g: r.get("deal_price") for g, r in games.items()}
        exempt = config.get("cap_exempt", "")
        self.exempt = re.compile(exempt, re.I) if exempt else None
        self.deal_match = re.compile(config.get("deal_match", "booster (box|display)"), re.I)
        self.hard_max = config.get("hard_max_usd", 0)

    def usd(self, price, currency):
        return price * self.fx.get(currency, 1.0)

    def suppress(self, price, currency):
        return bool(self.hard_max and price and self.usd(price, currency) > self.hard_max)

    def flag(self, game, title, price, currency):
        if not price or (self.exempt and self.exempt.search(title)):
            return ""
        usd = self.usd(price, currency)
        cap = self.caps.get(game)
        if cap and usd > cap:
            return f"⚠️ OVER LIMIT: ~{usd:.0f} USD vs your {cap:.0f} cap"
        deal = self.deals.get(game)
        if deal and usd <= deal and self.deal_match.search(title):
            return f"💎 DEAL: ~{usd:.0f} USD ≤ your {deal:.0f} target"
        return ""


class Alert:
    def __init__(self, kind, store, title, price, currency, url, detail="", game=""):
        self.kind = kind          # NEW LISTING | PREORDER/RESTOCK LIVE | PRICE CHANGE | PRICE DROP | STORE ERROR
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
            game, handle, title, price, available, cooldown_s):
    """Radar and sweep both funnel every observed product through here:
    registers new listings, detects availability flips and >10% price drops
    on known ones. Items over the hard ceiling update state but never alert;
    restock alerts respect the flap cooldown."""
    now = int(time.time())
    url = f"{surl}/products/{handle}"
    too_expensive = judge.suppress(price, cur)
    flag = judge.flag(game, title, price, cur)
    if handle in products:
        w = products[handle]
        prev_price = w.get("price", 0.0)
        if not w.get("delisted") and not too_expensive:
            if w.get("available") is False and available:
                if now - w.get("last_restock_alert", 0) >= cooldown_s:
                    detail = " · ".join(x for x in (w.get("note", ""), flag) if x)
                    alerts.append(Alert("PREORDER/RESTOCK LIVE", sname, title, price, cur, url,
                                        detail, game=game))
                    w["last_restock_alert"] = now
            elif available and prev_price and price and price < prev_price * 0.9:
                detail = " · ".join(x for x in (f"was {prev_price:.2f} {cur}", flag) if x)
                alerts.append(Alert("PRICE DROP", sname, title, price, cur, url,
                                    detail, game=game))
        w.update({"title": title, "available": available, "price": price})
        _track_unavail(w, available, now)
        fresh.add(handle)
        return
    w = {"title": title, "game": game, "available": available, "price": price}
    _track_unavail(w, available, now)
    products[handle] = w
    fresh.add(handle)
    if baselined and not too_expensive:
        status = "available NOW" if available else "listed but not yet buyable — now watching"
        detail = " · ".join(x for x in (f"{game} · {status}", flag) if x)
        alerts.append(Alert("NEW LISTING", sname, title, price, cur, url, detail, game=game))


def _track_unavail(w, available, now):
    if available:
        w.pop("unavail_since", None)
    else:
        w.setdefault("unavail_since", now)


def game_for(config, text):
    """First game whose include regex matches (and exclude doesn't)."""
    for game, rule in config.get("games", {}).items():
        if re.search(rule["include"], text, re.I):
            exc = rule.get("exclude")
            if exc and re.search(exc, text, re.I):
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


def post_embeds(webhook, embeds):
    for i in range(0, len(embeds), 10):  # Discord: max 10 embeds per message
        body = json.dumps({"embeds": embeds[i:i + 10]}).encode()
        req = urllib.request.Request(
            webhook, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX):
            pass
        time.sleep(1)


def send_discord(webhook, alerts):
    icons = {"NEW LISTING": "🆕", "PREORDER/RESTOCK LIVE": "🟢", "PRICE CHANGE": "💲",
             "PRICE DROP": "📉", "STORE ERROR": "🚨"}
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
    post_embeds(webhook, embeds)


def dispatch(config, alerts, dry_run):
    """Send one store's alerts immediately, grouped by target webhook."""
    for a in alerts:
        print("  " + a.text().replace("\n", "\n  "))
    if dry_run:
        return
    groups = {}
    for a in alerts:
        hook = webhook_for(config, a.game)
        if hook:
            groups.setdefault(hook, []).append(a)
    for hook, batch in groups.items():
        send_discord(hook, batch)


def process_store(store, config, st, seeds, judge):
    """Scan one store (radar or sweep + flip checks). Runs inside a worker
    thread; touches only this store's slice of the state. Returns the lines
    to print, alerts to send, warnings, and whether the store looks down."""
    surl, sname, cur = store["url"], store["name"], store.get("currency", "USD")
    do_sweep = config["_do_sweep"]
    delay = config.get("request_delay_ms", 250) / 1000.0
    cooldown_s = config.get("restock_cooldown_hours", 6) * 3600
    dormant_s = config.get("dormant_days", 60) * 86400
    now = int(time.time())
    products = st["products"]
    baselined = st["baselined"]
    fresh = set()
    alerts, errors, lines = [], [], []
    attempts = failures = 0

    # ---- sweep mode: page the store's full catalog (search only ever
    # shows the top 10 per query; this catches everything else) ----
    if do_sweep:
        match = re.compile(config.get("sweep_match", "booster (box|display)"), re.I)
        page = 1
        while page <= 20:
            attempts += 1
            try:
                data = http_json(f"{surl}/products.json?limit=250&page={page}")
            except Exception as e:
                failures += 1
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
                        game, handle, title, price, available, cooldown_s)
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
            attempts += 1
            try:
                results = search_products(surl, q_text)
            except Exception as e:
                failures += 1
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
                        game, handle, title, price, available, cooldown_s)

    # ---- pass 2: active flip checks for unavailable products ----
    for handle, seed in seeds.items():
        entry = products.setdefault(handle, {"title": handle, "available": None, "price": 0.0})
        entry["note"] = seed.get("note", "")
        if seed.get("game"):
            entry["game"] = seed["game"]

    for w in products.values():  # legacy entries get a dormancy clock
        if w.get("available") is False:
            w.setdefault("unavail_since", now)

    candidates = [h for h, w in products.items()
                  if h not in fresh and not w.get("delisted")
                  and w.get("available") is not True
                  and now - w.get("unavail_since", now) < dormant_s]
    # seeded watches are checked every run; the rest rotate through a
    # per-store cap so the run stays bounded as the tracked list grows.
    # Dormant items (unavailable > dormant_days) rely on the daily sweep.
    seeded_handles = set(seeds)
    must = [h for h in candidates if h in seeded_handles]
    rest = sorted(h for h in candidates if h not in seeded_handles)
    limit = config.get("max_active_checks_per_store", 40)
    cursor = st.get("check_cursor", 0)
    rotated = [rest[(cursor + i) % len(rest)] for i in range(min(limit, len(rest)))] if rest else []
    if rest:
        st["check_cursor"] = (cursor + len(rotated)) % len(rest)
    excludes = {g: re.compile(r["exclude"], re.I) for g, r in config["games"].items()
                if r.get("exclude")}
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
        game = w.get("game", "")
        price_flag = judge.flag(game, title, price, cur)
        note = " · ".join(x for x in (w.get("note", ""), price_flag) if x)
        exc = excludes.get(game)
        muted = judge.suppress(price, cur) or (exc and exc.search(title))

        if not muted:
            if prev_avail is False and available:
                if int(time.time()) - w.get("last_restock_alert", 0) >= cooldown_s:
                    alerts.append(Alert("PREORDER/RESTOCK LIVE", sname, title, price, cur, url,
                                        note, game=game))
                    w["last_restock_alert"] = int(time.time())
            elif prev_avail is not None and prev_price and price and abs(price - prev_price) / prev_price > 0.01:
                alerts.append(Alert("PRICE CHANGE", sname, title, price, cur, url,
                                    f"was {prev_price:.2f} {cur}" + (f" · {note}" if note else ""),
                                    game=game))
            elif prev_avail is not None and prev_price == 0.0 and price > 0:
                alerts.append(Alert("PRICE CHANGE", sname, title, price, cur, url,
                                    "price set (was TBA)" + (f" · {note}" if note else ""),
                                    game=game))

        w.update({"title": title, "available": available, "price": price})
        _track_unavail(w, available, int(time.time()))

    if not baselined:
        n_wait = sum(1 for w in products.values() if w.get("available") is False)
        lines.append(f"[baseline] {sname}: tracking {len(products)} products ({n_wait} awaiting availability)")
        st["baselined"] = True

    store_failed = attempts > 0 and failures == attempts
    st["last_error"] = errors[-1][-160:] if store_failed and errors else ""
    return sname, lines, alerts, errors, store_failed


def run(dry_run=False, do_sweep=False):
    config = load_json(CONFIG_PATH, None)
    if config is None:
        sys.exit(f"cannot read {CONFIG_PATH}")
    config["_do_sweep"] = do_sweep
    state = load_json(STATE_PATH, {"stores": {}})
    judge = PriceJudge(config)
    errors = []
    total_alerts = 0

    # WATCHER_PROFILE splits the fleet: "cloud" (GitHub Actions) takes stores
    # reachable from datacenter IPs; "local" (home PC) takes the ones whose
    # bot protection blocks cloud runners (config "cloud": false). Unset runs
    # everything (manual use).
    profile = os.environ.get("WATCHER_PROFILE", "").lower()
    stores = config["stores"]
    if profile == "cloud":
        stores = [s for s in stores if s.get("cloud", True)]
    elif profile == "local":
        stores = [s for s in stores if not s.get("cloud", True)]
    if not stores:
        sys.exit(f"no stores match WATCHER_PROFILE={profile!r}")
    print(f"profile: {profile or 'all'} — {len(stores)} store(s)")

    seeded = {}
    for w in config.get("watched", []):
        seeded.setdefault(w["store"], {})[w["handle"]] = w

    # pre-create each store's state slice so workers never touch shared dicts
    slices = {}
    for store in stores:
        slices[store["url"]] = state["stores"].setdefault(
            store["url"], {"products": {}, "baselined": False})

    with ThreadPoolExecutor(max_workers=min(8, len(stores))) as pool:
        futures = {
            pool.submit(process_store, store, config, slices[store["url"]],
                        seeded.get(store["url"], {}), judge): store
            for store in stores
        }
        outcomes = []
        for fut in as_completed(futures):
            store = futures[fut]
            st = slices[store["url"]]
            try:
                sname, lines, alerts, errs, store_failed = fut.result()
            except Exception as e:
                errors.append(f"{store['name']}: worker crashed: {e}")
                continue
            for line in lines:
                print(line)
            errors.extend(errs)
            outcomes.append((store, st, sname, store_failed))
            if alerts:
                total_alerts += len(alerts)
                dispatch(config, alerts, dry_run)  # ship immediately, per store

    # streak accounting happens after the run: if most stores failed at once,
    # the problem is on our side (runner IP throttled/blocked) — don't count
    # it against individual stores or page the user about each one
    failed = [o for o in outcomes if o[3]]
    if outcomes and len(failed) > len(outcomes) / 2:
        print(f"[warn] {len(failed)}/{len(outcomes)} stores failed this run — "
              "treating as runner-side, streaks unchanged", file=sys.stderr)
    else:
        late_alerts = []
        for store, st, sname, store_failed in outcomes:
            streak = st.get("error_streak", 0)
            st["error_streak"] = streak + 1 if store_failed else 0
            if st["error_streak"] == 3:
                late_alerts.append(Alert("STORE ERROR", sname, f"{sname} unreachable",
                                         0, "USD", store["url"],
                                         "3 consecutive failed runs — store may be down, "
                                         "blocked, or no longer on Shopify"))
        if late_alerts:
            total_alerts += len(late_alerts)
            dispatch(config, late_alerts, dry_run)

    if total_alerts:
        print(f"\n{total_alerts} alert(s) this run" + (" (dry run, not sent)" if dry_run else ""))
    else:
        print("no alerts this run")

    for e in errors:
        print(f"[warn] {e}", file=sys.stderr)

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def digest(dry_run=False):
    """Per-game value board: cheapest in-stock booster boxes across all
    stores, plus a health summary. Run daily after the sweep."""
    config = load_json(CONFIG_PATH, None)
    if config is None:
        sys.exit(f"cannot read {CONFIG_PATH}")
    state = load_json(STATE_PATH, {"stores": {}})
    judge = PriceJudge(config)
    top_n = config.get("digest_top_n", 8)
    store_meta = {s["url"]: (s["name"], s.get("currency", "USD")) for s in config["stores"]}
    excludes = {g: re.compile(r["exclude"], re.I) for g, r in config["games"].items()
                if r.get("exclude")}

    for game in config["games"]:
        rows = []
        exc = excludes.get(game)
        for surl, st in state["stores"].items():
            sname, cur = store_meta.get(surl, (surl, "USD"))
            for handle, w in st["products"].items():
                if w.get("game") != game or w.get("available") is not True or w.get("delisted"):
                    continue
                title, price = w.get("title", handle), w.get("price", 0.0)
                if not price or not judge.deal_match.search(title):
                    continue
                if exc and exc.search(title):
                    continue
                usd = judge.usd(price, cur)
                if judge.hard_max and usd > judge.hard_max:
                    continue
                rows.append((usd, title, sname, f"{surl}/products/{handle}"))
        rows.sort(key=lambda r: r[0])
        if not rows:
            continue
        body = "\n".join(f"**~${usd:.0f}** — [{title[:80]}]({url}) @ {sname}"
                         for usd, title, sname, url in rows[:top_n])
        embed = {"title": f"📊 {game}: cheapest in-stock booster boxes",
                 "description": body[:4000]}
        print(f"\n{embed['title']}\n{body}")
        hook = webhook_for(config, game)
        if hook and not dry_run:
            post_embeds(hook, [embed])

    # health summary to the default webhook
    total = sum(len(st["products"]) for st in state["stores"].values())
    active = sum(1 for st in state["stores"].values() for w in st["products"].values()
                 if w.get("available") is False and not w.get("delisted"))
    failing = [f"{store_meta.get(u, (u,))[0]} — {st.get('last_error', 'no error recorded')[-90:]}"
               for u, st in state["stores"].items() if st.get("error_streak", 0) >= 3]
    status = f"tracking {total} products · {active} awaiting availability across {len(state['stores'])} stores"
    if failing:
        status += "\n🚨 failing stores:\n" + "\n".join(failing)
    print(f"\n🩺 {status}")
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if hook and not dry_run:
        post_embeds(hook, [{"title": "🩺 Watcher health", "description": status[:4000]}])


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
    ap.add_argument("--digest", action="store_true",
                    help="post the per-game value board + health summary (run daily)")
    args = ap.parse_args()
    if args.test_alert:
        test_alert()
    elif args.digest:
        digest(dry_run=args.dry_run)
    else:
        run(dry_run=args.dry_run, do_sweep=args.sweep)

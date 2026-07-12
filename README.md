# TCG Restock Watcher

![watcher status](https://github.com/sm1ttayy/CardShopStockAlerts/actions/workflows/watch.yml/badge.svg)

Watches 15 vetted Shopify TCG stores for **new-set preorder listings** and
**restocks** (One Piece, Pokémon, Riftbound, MTG) and pings a Discord webhook.

## How it works

Every run, per store:

1. **Radar** — keyword searches (`search/suggest.json`) find new sealed-product
   listings matching the rules in `config.json`. New matches fire a
   `NEW LISTING` alert and are auto-added to the watched list.
2. **Restock check** — every watched product (`/products/<handle>.js`) is
   checked; `available: false → true` fires `PREORDER/RESTOCK LIVE`, and price
   changes (including "price TBA → real price") fire `PRICE CHANGE`.

The first run per store is a **silent baseline** (no alert spam for products
that already exist). State is stored in `state.json`, which the GitHub Actions
workflow commits back after each run.

## Setup (one time)

1. **Discord webhook**: in a server you control → Server Settings →
   Integrations → Webhooks → New Webhook → Copy URL.
2. **Create a GitHub repo** (make it **public** — see Notes on Actions
   minutes) and push this folder:
   ```
   git init
   git add .
   git commit -m "initial watcher"
   git remote add origin https://github.com/<you>/tcg-restock-watcher.git
   git push -u origin main
   ```
3. **Add the webhook as a secret**: repo → Settings → Secrets and variables →
   Actions → New repository secret → name `DISCORD_WEBHOOK_URL`, value = the URL.

   **Per-game channels (optional)**: create a webhook in each game's channel
   and add it as a secret named per `webhook_env` in `config.json`
   (`DISCORD_WEBHOOK_ONE_PIECE`, `DISCORD_WEBHOOK_POKEMON`,
   `DISCORD_WEBHOOK_RIFTBOUND`, `DISCORD_WEBHOOK_MTG`). Games without their
   own secret fall back to `DISCORD_WEBHOOK_URL`. Verify the routing with
   `python watcher.py --test-alert` — it sends one labeled test per channel.
4. **Enable the workflow**: Actions tab → enable workflows → run
   "TCG Restock Watcher" once manually (Run workflow) to baseline.

## Local usage

```
python watcher.py --dry-run     # full run, print alerts, never post
python watcher.py --test-alert  # send one sample alert to verify the webhook
python watcher.py               # real run (posts if DISCORD_WEBHOOK_URL is set)
```

## Tuning

- **Add a store**: append `{name, url, currency}` to `stores` in `config.json`.
  Any Shopify store works — test with `<url>/products.json?limit=1`.
- **Watch a specific product**: add `{store, handle, note}` to `watched`
  (the handle is the last part of the product URL).
- **Keywords/filters**: per game in `games` — `queries` (search terms),
  `include`/`exclude` (regex on title), `min_price` (filters out singles
  and packs).
- **Price caps**: `max_price` per game (in USD). Alerts above the cap are
  never hidden, just flagged ⚠️ OVER LIMIT. Canadian store prices are
  converted using `fx_to_usd` (update the CAD rate occasionally). Titles
  matching `cap_exempt` (default: cases) are never flagged, since a
  multi-box case legitimately costs several times a single box.

## Notes

- **Use a public repo.** A run takes ~2 minutes; at a 10-minute cadence that is
  ~9,000 Actions minutes/month — far past the 2,000/month free tier for
  *private* repos, but unlimited and free for public ones. Nothing sensitive
  is in the code; the webhook URL lives in repo Secrets. If you must go
  private, change the cron to `*/30`.
- GitHub cron is best-effort: expect 10–20 min real cadence, occasionally more.
- Expect a trickle of `NEW LISTING` alerts in the first days: search rankings
  shift between runs, so older products keep surfacing until they've all been
  seen once. It quiets down on its own.
- GitHub disables scheduled workflows after **60 days without repo activity**;
  the state-commit on each run keeps it alive automatically.
- Be polite: keep `request_delay_ms` ≥ 250 and the cadence ≥ 10 min.

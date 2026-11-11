"""
Figure Watch - checks Japanese shops and marketplaces for the figures listed
in figures.yaml and posts new listings / restocks / price drops to Discord.

Sites: Mercari JP, Yahoo Auctions, Suruga-ya, Rakuten Ichiba (optional).

Run modes:
    python watch.py                 normal check (sends alerts, saves state)
    python watch.py --dry-run       check and print results, send/save nothing
    python watch.py --test-discord  just send a test message to Discord
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "figures.yaml"
STATE_FILE = ROOT / "state" / "seen.json"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SITE_NAMES = {
    "mercari": "Mercari",
    "yahoo": "Yahoo Auctions",
    "surugaya": "Suruga-ya",
    "rakuten": "Rakuten",
}
SITE_COLOURS = {
    "mercari": 0xFF0211,
    "yahoo": 0x7B0099,
    "surugaya": 0x0066CC,
    "rakuten": 0xBF0000,
}

# Something that has vanished from results for this long and then comes back
# counts as "back in stock" rather than "already seen".
REAPPEAR_AFTER_SECS = 6 * 3600
# Forget items not seen for this long (keeps the state file small).
FORGET_AFTER_SECS = 30 * 24 * 3600
# Warn on Discord after a site has failed this many runs in a row (~3 hours).
FAILURE_WARN_AFTER = 6

PRICE_RE = re.compile(r"[￥¥]\s*([\d,]+)|([\d,]+)\s*円")


def log(msg):
    print(msg, flush=True)


def now_ts():
    return int(time.time())


def parse_price(text):
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    m = PRICE_RE.search(str(text))
    if m:
        return int((m.group(1) or m.group(2)).replace(",", ""))
    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else None


# ---------------------------------------------------------------------------
# Generic HTML search-result parser.
# Finds every link to an item page, then climbs up the page to the biggest
# block that still only belongs to that one item, and reads the price,
# title and picture from it. This survives most site redesigns because it
# only depends on what item links look like, not on CSS class names.
# ---------------------------------------------------------------------------

def extract_items(html, base_url, id_pattern, max_depth=8):
    soup = BeautifulSoup(html, "html.parser")

    def item_id_of(a):
        href = a.get("href")
        if not href:
            return None
        m = id_pattern.search(urljoin(base_url, href))
        return m.group(1) if m else None

    anchors = {}
    order = []
    for a in soup.find_all("a", href=True):
        iid = item_id_of(a)
        if iid:
            if iid not in anchors:
                anchors[iid] = []
                order.append(iid)
            anchors[iid].append(a)

    items = []
    for iid in order:
        first = anchors[iid][0]
        node = first
        container = first
        for _ in range(max_depth):
            parent = node.parent
            if parent is None or parent.name in ("body", "html", "[document]"):
                break
            ids = {item_id_of(a) for a in parent.find_all("a", href=True)}
            ids.discard(None)
            if len(ids) > 1:
                break
            node = parent
            container = parent

        text = container.get_text(" ", strip=True)
        price = parse_price(text)

        title = ""
        for a in anchors[iid]:
            t = a.get_text(" ", strip=True) or a.get("title", "")
            if len(t) > len(title):
                title = t
        img = container.find("img")
        if not title and img is not None:
            title = img.get("alt", "")
        image = None
        if img is not None:
            image = img.get("data-src") or img.get("data-original") or img.get("src")
            if image and image.startswith("data:"):
                image = None
            if image:
                image = urljoin(base_url, image)

        items.append({
            "id": iid,
            "title": title.strip(),
            "price": price,
            "url": urljoin(base_url, first["href"]),
            "image": image,
            "text": text,
        })
    return items


def looks_blocked(resp):
    if resp.status_code in (403, 429, 503):
        return True
    head = resp.text[:3000].lower()
    return "just a moment" in head or "cf-chl" in head or "captcha" in head


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------

class SiteBlocked(Exception):
    pass


def search_yahoo(session, query):
    url = (
        "https://auctions.yahoo.co.jp/search/search?p={q}&va={q}"
        "&s1=new&o1=d&n=50&b=1&exflg=1"
    ).format(q=quote(query))
    resp = session.get(url, headers=HEADERS, timeout=30)
    if looks_blocked(resp) or "ご利用いただけません" in resp.text[:5000]:
        raise SiteBlocked(f"Yahoo blocked the request (HTTP {resp.status_code})")
    resp.raise_for_status()
    pattern = re.compile(r"auctions\.yahoo\.co\.jp/jp/auction/([A-Za-z]?\d{6,})")
    return extract_items(resp.text, url, pattern)


def search_surugaya(session, query):
    url = (
        "https://www.suruga-ya.jp/search?category=&search_word={q}"
        "&inStock=On&rankBy=modificationTime%3Adescending"
    ).format(q=quote(query))
    resp = session.get(url, headers=HEADERS, timeout=30)
    if looks_blocked(resp):
        raise SiteBlocked(f"Suruga-ya blocked the request (HTTP {resp.status_code})")
    resp.raise_for_status()
    pattern = re.compile(r"suruga-ya\.jp/product/detail/([\w-]+)")
    items = extract_items(resp.text, url, pattern)
    # Belt and braces: drop anything the page itself says is sold out.
    return [i for i in items if "品切れ" not in i["text"] or i["price"]]


class Mercari:
    """Talks to the same search API the jp.mercari.com website uses.
    The site signs each request with a throwaway key (DPoP), so we do too."""

    API = "https://api.mercari.jp/v2/entities:search"

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        self._key = ec.generate_private_key(ec.SECP256R1())
        nums = self._key.public_key().public_numbers()
        self._jwk = {
            "crv": "P-256",
            "kty": "EC",
            "x": _b64(nums.x.to_bytes(32, "big")),
            "y": _b64(nums.y.to_bytes(32, "big")),
        }
        self._session_uuid = str(uuid.uuid4())

    def _dpop(self, method, url):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self._jwk}
        payload = {
            "iat": now_ts(),
            "jti": str(uuid.uuid4()),
            "htu": url,
            "htm": method,
            "uuid": self._session_uuid,
        }
        signing_input = (
            _b64(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64(json.dumps(payload, separators=(",", ":")).encode())
        )
        der = self._key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return signing_input + "." + _b64(sig)

    def search(self, session, query):
        body = {
            "userId": "",
            "pageSize": 60,
            "pageToken": "",
            "searchSessionId": uuid.uuid4().hex,
            "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
            "thumbnailTypes": [],
            "searchCondition": {
                "keyword": query,
                "excludeKeyword": "",
                "sort": "SORT_CREATED_TIME",
                "order": "ORDER_DESC",
                "status": ["STATUS_ON_SALE"],
                "sizeId": [], "categoryId": [], "brandId": [], "sellerId": [],
                "priceMin": 0, "priceMax": 0,
                "itemConditionId": [], "shippingPayerId": [], "shippingFromArea": [],
                "shippingMethod": [], "colorId": [], "hasCoupon": False,
                "attributes": [], "itemTypes": [], "skuIds": [],
            },
            "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"],
            "serviceFrom": "suruga",
            "withItemBrand": True,
            "withItemSize": False,
            "withItemPromotions": True,
            "withItemSizes": True,
            "withShopname": False,
        }
        headers = {
            "User-Agent": BROWSER_UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://jp.mercari.com",
            "Referer": "https://jp.mercari.com/",
            "X-Platform": "web",
            "DPoP": self._dpop("POST", self.API),
        }
        resp = session.post(self.API, headers=headers, json=body, timeout=30)
        if resp.status_code in (401, 403, 429):
            raise SiteBlocked(f"Mercari refused the request (HTTP {resp.status_code}): {resp.text[:200]}")
        resp.raise_for_status()
        out = []
        for it in resp.json().get("items", []):
            status = str(it.get("status", ""))
            if status and "ON_SALE" not in status:
                continue
            iid = str(it.get("id", ""))
            if not iid:
                continue
            is_shop = it.get("itemType") == "ITEM_TYPE_BEYOND" or not iid.startswith("m")
            link = (
                f"https://jp.mercari.com/shops/product/{iid}" if is_shop
                else f"https://jp.mercari.com/item/{iid}"
            )
            thumbs = it.get("thumbnails") or []
            out.append({
                "id": iid,
                "title": it.get("name", ""),
                "price": parse_price(it.get("price")),
                "url": link,
                "image": thumbs[0] if thumbs else None,
                "text": it.get("name", ""),
            })
        return out


def search_rakuten(session, query, max_price=None):
    app_id = os.environ.get("RAKUTEN_APP_ID")
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY")
    referer = os.environ.get("RAKUTEN_REFERER")
    if not (app_id and access_key and referer):
        return None  # Rakuten not set up -> skipped quietly
    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "keyword": query,
        "availability": 1,
        "sort": "-updateTimestamp",
        "hits": 30,
        "formatVersion": 2,
    }
    if max_price:
        params["maxPrice"] = int(max_price)
    resp = session.get(
        "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601",
        params=params,
        headers={"Referer": referer, "User-Agent": BROWSER_UA},
        timeout=30,
    )
    if resp.status_code in (401, 403):
        raise SiteBlocked(f"Rakuten rejected the keys (HTTP {resp.status_code}): {resp.text[:200]}")
    if resp.status_code == 404 and "not_found" in resp.text:
        return []  # Rakuten answers 404 when there are simply no results
    resp.raise_for_status()
    out = []
    for it in resp.json().get("Items", []):
        it = it.get("Item", it)  # works with formatVersion 1 or 2
        if it.get("availability") == 0:
            continue
        imgs = it.get("mediumImageUrls") or []
        img = imgs[0] if imgs else None
        if isinstance(img, dict):
            img = img.get("imageUrl")
        out.append({
            "id": it.get("itemCode", it.get("itemUrl")),
            "title": it.get("itemName", ""),
            "price": parse_price(it.get("itemPrice")),
            "url": it.get("itemUrl"),
            "image": img,
            "text": it.get("itemName", ""),
        })
    return out


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def normalise(s):
    # Full-width letters/numbers -> normal, lower case, no spaces.
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).lower()


def matches(item, figure, global_exclude):
    title = normalise(item["title"])
    for word in list(figure.get("exclude") or []) + list(global_exclude or []):
        if normalise(word) and normalise(word) in title:
            return False
    for word in figure.get("must_include") or []:
        if normalise(word) not in title:
            return False
    any_of = figure.get("include_any") or []
    if any_of and not any(normalise(w) in title for w in any_of):
        return False
    max_price = figure.get("max_price")
    if max_price and item["price"] and item["price"] > int(max_price):
        return False
    return True


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    data.setdefault("items", {})
    data.setdefault("baselined", [])
    data.setdefault("failures", {})
    return data


def save_state(state):
    cutoff = now_ts() - FORGET_AFTER_SECS
    state["items"] = {k: v for k, v in state["items"].items() if v.get("last", 0) >= cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def diff_results(state, figure_name, site, items, ts):
    """Updates state with this run's results and returns (alerts, baselining)."""
    combo = f"{figure_name}|{site}"
    baselining = combo not in state["baselined"]
    alerts = []
    for item in items:
        key = f"{combo}|{item['id']}"
        prev = state["items"].get(key)
        if prev is None:
            if not baselining:
                alerts.append(("new", item, None))
        else:
            gone_for = ts - prev.get("last", ts)
            old_price = prev.get("price")
            if gone_for >= REAPPEAR_AFTER_SECS:
                alerts.append(("back", item, None))
            elif (
                old_price and item["price"] and item["price"] < old_price
                and site != "yahoo"  # auction prices go up, not down
            ):
                alerts.append(("drop", item, old_price))
        state["items"][key] = {
            "first": prev["first"] if prev else ts,
            "last": ts,
            "price": item["price"],
        }
    if baselining:
        state["baselined"].append(combo)
    return alerts, baselining


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def get_gbp_rate():
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest?from=JPY&to=GBP", timeout=10
        )
        return float(r.json()["rates"]["GBP"])
    except Exception:
        return None


def fmt_price(price, gbp_rate):
    if not price:
        return "price not shown"
    s = f"¥{price:,}"
    if gbp_rate:
        s += f" (≈ £{price * gbp_rate:,.0f})"
    return s


class Discord:
    def __init__(self, webhook, dry_run=False):
        self.webhook = webhook
        self.dry_run = dry_run

    def send(self, content=None, embeds=None):
        payload = {"username": "Figure Watch"}
        if content:
            payload["content"] = content[:2000]
        if embeds:
            payload["embeds"] = embeds
        if self.dry_run or not self.webhook:
            log("[discord] " + json.dumps(payload, ensure_ascii=False)[:600])
            return
        for _ in range(5):
            r = requests.post(self.webhook, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            if r.status_code >= 400:
                log(f"Discord error {r.status_code}: {r.text[:300]}")
            return
        log("Discord kept rate limiting; giving up on this message.")


LABELS = {"new": "New listing", "back": "Back in stock", "drop": "Price drop"}


def build_embed(kind, item, site, old_price, gbp_rate):
    price_line = fmt_price(item["price"], gbp_rate)
    if kind == "drop":
        price_line = f"~~¥{old_price:,}~~ → {price_line}"
    embed = {
        "title": (item["title"] or "(no title)")[:250],
        "url": item["url"],
        "description": f"**{price_line}**",
        "color": SITE_COLOURS[site],
        "footer": {"text": f"{SITE_NAMES[site]} · {LABELS[kind]}"},
    }
    if item.get("image"):
        embed["thumbnail"] = {"url": item["image"]}
    return embed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_config():
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    figures = cfg.get("figures") or []
    for f in figures:
        if not f.get("name") or not (f.get("search") or f.get("jan")):
            raise SystemExit(f"Each figure needs a name and a search (or jan): {f}")
    return cfg, figures


def run(dry_run=False):
    cfg, figures = load_config()
    settings = cfg.get("settings") or {}
    default_sites = settings.get("sites") or list(SITE_NAMES)
    global_exclude = settings.get("exclude") or []
    pause = float(settings.get("pause_seconds", 3))

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook and not dry_run:
        raise SystemExit("DISCORD_WEBHOOK_URL is not set (add it as a GitHub secret).")
    discord = Discord(webhook, dry_run=dry_run)
    gbp_rate = get_gbp_rate() if settings.get("show_gbp", True) else None

    state = load_state()
    ts = now_ts()
    session = requests.Session()
    mercari = Mercari()
    failed_sites = {}
    summary = []

    for fig in figures:
        name = fig["name"]
        sites = fig.get("sites") or default_sites
        fig_embeds = []
        baseline_counts = []

        for site in sites:
            if site not in SITE_NAMES:
                log(f"Unknown site '{site}' for {name} - skipping")
                continue
            if site in failed_sites:
                continue  # don't keep hammering a site that's blocking us
            # JAN barcodes work well on shops; marketplaces need words.
            query = fig.get("search")
            if fig.get("jan") and (site in ("surugaya", "rakuten") or not query):
                query = str(fig["jan"])
            try:
                if site == "mercari":
                    found = mercari.search(session, query)
                elif site == "yahoo":
                    found = search_yahoo(session, query)
                elif site == "surugaya":
                    found = search_surugaya(session, query)
                else:
                    found = search_rakuten(session, query, fig.get("max_price"))
                    if found is None:
                        continue
            except Exception as e:  # noqa: BLE001 - one bad site shouldn't stop the rest
                failed_sites[site] = f"{type(e).__name__}: {e}"
                log(f"  ! {SITE_NAMES[site]} failed: {failed_sites[site]}")
                continue
            finally:
                time.sleep(pause)

            # When searching by JAN the title won't contain the words, so
            # only apply price / exclude filters in that case.
            fig_for_match = dict(fig)
            if query == str(fig.get("jan")):
                fig_for_match["must_include"] = []
                fig_for_match["include_any"] = []
            good = [i for i in found if matches(i, fig_for_match, global_exclude)]
            log(f"{name} @ {SITE_NAMES[site]}: {len(found)} results, {len(good)} match")
            summary.append((name, site, len(found), len(good)))

            alerts, baselining = diff_results(state, name, site, good, ts)
            if baselining:
                cheapest = min((i["price"] for i in good if i["price"]), default=None)
                baseline_counts.append(
                    f"{SITE_NAMES[site]}: {len(good)} listed"
                    + (f", cheapest {fmt_price(cheapest, gbp_rate)}" if cheapest else "")
                )
            for kind, item, old in alerts:
                fig_embeds.append(build_embed(kind, item, site, old, gbp_rate))

        if baseline_counts:
            discord.send(
                f"👀 Now watching **{name}**. What's listed right now (you'll only "
                "be pinged about new ones from here on):\n• " + "\n• ".join(baseline_counts)
            )
        for i in range(0, len(fig_embeds), 10):
            discord.send(content=f"🔔 **{name}**", embeds=fig_embeds[i:i + 10])

    # Failure tracking: warn once if a site keeps failing.
    for site in SITE_NAMES:
        if site in failed_sites:
            count = state["failures"].get(site, 0) + 1
            state["failures"][site] = count
            if count == FAILURE_WARN_AFTER:
                discord.send(
                    f"⚠️ {SITE_NAMES[site]} has failed {count} checks in a row. "
                    f"Last error: `{failed_sites[site][:300]}`"
                )
        elif site in state["failures"]:
            if state["failures"][site] >= FAILURE_WARN_AFTER:
                discord.send(f"✅ {SITE_NAMES[site]} is working again.")
            del state["failures"][site]

    if not dry_run:
        save_state(state)
    log(f"Done. {len(summary)} searches, {len(failed_sites)} site(s) failed.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--test-discord", action="store_true")
    p.add_argument("--mode", choices=["normal", "dry-run", "test-discord"], default=None)
    args = p.parse_args()
    mode = args.mode or ("test-discord" if args.test_discord else "dry-run" if args.dry_run else "normal")

    if mode == "test-discord":
        webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        if not webhook:
            raise SystemExit("DISCORD_WEBHOOK_URL is not set.")
        Discord(webhook).send(
            "✅ Figure Watch is connected. Alerts for your figures will show up here."
        )
        log("Test message sent.")
        return
    run(dry_run=(mode == "dry-run"))


if __name__ == "__main__":
    sys.exit(main())

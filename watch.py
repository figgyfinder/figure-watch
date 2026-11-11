"""
Figure Watch - checks Japanese shops and marketplaces for the figures listed
in figures.yaml and posts new listings / restocks / price drops to Discord.

Sites: Mercari JP, Yahoo Auctions, Suruga-ya, AmiAmi, Mandarake,
Rakuten Ichiba (optional).

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
    "yahooflea": "Yahoo Flea Market",
    "rakuma": "Rakuma",
    "surugaya": "Suruga-ya",
    "amiami": "AmiAmi",
    "mandarake": "Mandarake",
    "rakuten": "Rakuten",
}
SITE_COLOURS = {
    "mercari": 0xFF0211,
    "yahoo": 0x7B0099,
    "yahooflea": 0xFF0033,
    "rakuma": 0xE6006F,
    "surugaya": 0x0066CC,
    "amiami": 0xF39800,
    "mandarake": 0x2E7D32,
    "rakuten": 0xBF0000,
}

# Something that has vanished from results for this long and then comes back
# counts as "back in stock" rather than "already seen".
REAPPEAR_AFTER_SECS = 6 * 3600
# Forget items not seen for this long (keeps the state file small).
FORGET_AFTER_SECS = 30 * 24 * 3600
# Wait this long before retrying a failed search, and give up on a site for
# the rest of the run after this many failed searches.
RETRY_WAIT = 15
GIVE_UP_AFTER = 3
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


def search_yahoo(session, query, kind=None):
    url = (
        "https://auctions.yahoo.co.jp/search/search?p={q}&va={q}"
        "&s1=new&o1=d&n=50&b=1&exflg=1"
    ).format(q=quote(query))
    if kind == "scale":
        url += "&auccat=25888"  # Yahoo's figure category
    resp = session.get(url, headers=HEADERS, timeout=30)
    if looks_blocked(resp) or "ご利用いただけません" in resp.text[:5000]:
        raise SiteBlocked(f"Yahoo blocked the request (HTTP {resp.status_code})")
    resp.raise_for_status()
    # With no exact results Yahoo shows "related" listings under a notice;
    # those are the loose junk matches, so treat it as no results.
    if "Notice__wandText" in resp.text or "に一致する商品はありません" in resp.text:
        return []
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

    def search(self, session, query, kind=None):
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
                "sizeId": [], "categoryId": [81] if kind == "scale" else [],
                "brandId": [], "sellerId": [],
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


def search_amiami(session, query, kind=None):
    """AmiAmi's own website search API (new, pre-order and pre-owned)."""
    resp = session.get(
        "https://api.amiami.com/api/v1.0/items",
        params={
            "pagemax": 30, "pagecnt": 1, "lang": "jpn", "s_keywords": query,
            # include 18+ items (several watched figures are) and all
            # orderable stock: new, pre-order, back-order and pre-owned
            "age_confirm": "true",
            "s_st_list_preorder_available": 1,
            "s_st_list_backorder_available": 1,
            "s_st_list_newitem_available": 1,
            "s_st_condition_flg": 1,
        },
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
            "Referer": "https://www.amiami.com/",
            "Origin": "https://www.amiami.com",
            "X-User-Key": "amiami_dev",
        },
        timeout=30,
    )
    if looks_blocked(resp):
        raise SiteBlocked(f"AmiAmi blocked the request (HTTP {resp.status_code})")
    resp.raise_for_status()
    data = resp.json()
    out = []
    for it in data.get("items") or []:
        if it.get("order_closed_flg") == 1:
            continue
        if not any(it.get(k) == 1 for k in ("instock_flg", "preorderitem", "backorderitem")):
            if any(k in it for k in ("instock_flg", "preorderitem", "backorderitem")):
                continue  # AmiAmi says it can't be ordered right now
        code = it.get("scode") or it.get("gcode")
        if not code:
            continue
        key = "scode" if it.get("scode") else "gcode"
        thumb = it.get("thumb_url")
        if thumb and thumb.startswith("/"):
            thumb = "https://img.amiami.com" + thumb
        title = it.get("gname", "")
        if it.get("preorderitem") == 1:
            title = "[Pre-order] " + title
        out.append({
            "id": code,
            "title": title,
            "price": parse_price(it.get("c_price_taxed") or it.get("min_price")),
            "url": f"https://www.amiami.com/eng/detail/?{key}={code}",
            "image": thumb,
            "text": title,
        })
    return out


def search_mandarake(session, query, kind=None):
    category = {"kit": "020107", "scale": "02", "doll": "02"}.get(kind, "")
    url = "https://order.mandarake.co.jp/order/listPage/list?soldOut=1&keyword={q}".format(q=quote(query))
    if category:
        url += f"&categoryCode={category}"
    resp = session.get(url, headers=HEADERS, timeout=30)
    if looks_blocked(resp):
        raise SiteBlocked(f"Mandarake blocked the request (HTTP {resp.status_code})")
    resp.raise_for_status()
    # 18+ items have no normal link: Mandarake puts the item code in the id
    # of an "adult_link" element instead. Give those a real link.
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.select("a.adult_link[id]"):
        if a["id"].isdigit():
            a["href"] = f"/order/detailPage/item?itemCode={a['id']}"
    pattern = re.compile(r"mandarake\.co\.jp/order/detailPage/item\?itemCode=(\d+)")
    items = extract_items(str(soup), url, pattern)
    sold = ("売り切れ", "SOLD OUT", "Sold out", "在庫なし")
    return [i for i in items if not any(s in i["text"] for s in sold)]


def search_yahoo_flea(session, query):
    """Yahoo! Flea Market (formerly PayPay Flea Market)."""
    api = "https://paypayfleamarket.yahoo.co.jp/api/v1/search"
    try:
        resp = session.get(
            api,
            params={
                "query": query, "results": 50, "imageShape": "square",
                "sort": "openTime", "order": "desc", "webp": "false",
                "itemStatus": "open",
            },
            headers={**HEADERS, "Accept": "application/json",
                     "Referer": "https://paypayfleamarket.yahoo.co.jp/"},
            timeout=30,
        )
        if resp.status_code == 200 and resp.text.lstrip().startswith("{"):
            out = []
            for it in resp.json().get("items") or []:
                if str(it.get("itemStatus", "OPEN")).upper() != "OPEN":
                    continue
                iid = str(it.get("id", ""))
                if not iid:
                    continue
                out.append({
                    "id": iid,
                    "title": it.get("title", ""),
                    "price": parse_price(it.get("price")),
                    "url": f"https://paypayfleamarket.yahoo.co.jp/item/{iid}",
                    "image": it.get("thumbnailImageUrl") or it.get("thumbnail"),
                    "text": it.get("title", ""),
                })
            return out
    except (requests.RequestException, ValueError):
        pass
    # Fallback: read the normal search page.
    url = "https://paypayfleamarket.yahoo.co.jp/search/{q}?open=1".format(q=quote(query))
    resp = session.get(url, headers=HEADERS, timeout=30)
    if looks_blocked(resp):
        raise SiteBlocked(f"Yahoo Flea Market blocked the request (HTTP {resp.status_code})")
    resp.raise_for_status()
    pattern = re.compile(r"paypayfleamarket\.yahoo\.co\.jp/item/(\w+)")
    items = extract_items(resp.text, url, pattern)
    return [i for i in items if "SOLD" not in i["text"].upper()]


RAKUMA_FIGURE_CATEGORIES = ["フィギュア", "ドール", "人形", "模型", "プラモデル", "キット"]


def search_rakuma(session, query):
    url = (
        "https://fril.jp/s?query={q}&transaction=selling&sort=created_at&order=desc"
    ).format(q=quote(query))
    resp = session.get(url, headers=HEADERS, timeout=30)
    if looks_blocked(resp):
        raise SiteBlocked(f"Rakuma blocked the request (HTTP {resp.status_code})")
    resp.raise_for_status()
    pattern = re.compile(r"item\.fril\.jp/([0-9a-f]{16,})")
    items = extract_items(resp.text, url, pattern)
    kept = []
    for i in items:
        # Rakuma link text is "<title> <category>の商品詳細ページへのリンク",
        # e.g. "... エンタメ/ホビーのフィギュアの商品詳細ページへのリンク".
        m = re.search(r"\s*(\S*)の商品詳細ページへのリンク\s*$", i["title"])
        category = m.group(1) if m else ""
        i["title"] = i["title"][:m.start()].strip() if m else i["title"].strip()
        if "SOLD" in i["text"].upper() or "売り切れ" in i["text"]:
            continue
        # Use Rakuma's own category: only figures, dolls and models count.
        if category and not any(w in category for w in RAKUMA_FIGURE_CATEGORIES):
            continue
        kept.append(i)
    return kept


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

_PUNCT = re.compile(r"[\s・･、。，,.!！?？~～〜「」『』()（）【】\[\]<>＜＞《》\"'`♡♥☆★※:：;；|｜&＆+＋_#＃-]")


def normalise(s):
    """Full-width -> normal, hiragana -> katakana, lower case, no spaces or
    punctuation (but "/" is kept so scales like 1/7 survive)."""
    import unicodedata
    s = unicodedata.normalize("NFKC", s or "")
    s = "".join(chr(ord(c) + 0x60) if "\u3041" <= c <= "\u3096" else c for c in s)
    return _PUNCT.sub("", s).lower()


_BONUS = re.compile(r"[^\s()（）【】\[\]]*(?:付き|付|つき|おまけ)")


def strip_bonus(title):
    """Remove "comes with X" phrases (特典ポストカード付, 箱付き...) so a real
    figure isn't thrown out as merch because of a bonus item."""
    return _BONUS.sub(" ", title or "")


def _groups(words):
    """["a","b"] -> [["a","b"]];  [["a"],["b","c"]] stays as is."""
    if not words:
        return []
    return words if isinstance(words[0], list) else [words]


def _has_each_group(title, groups):
    return all(any(normalise(w) in title for w in g) for g in groups)


def _flat(words):
    out = []
    for w in words or []:
        out.extend(_flat(w) if isinstance(w, list) else [str(w)])
    return out


def contradiction(item, figure):
    """Return a reason if the title shows this is a DIFFERENT item
    (another product line, maker, scale, outfit, bootleg, doll clothes...)."""
    raw = strip_bonus(item["title"])
    title = normalise(raw)
    own = normalise(" ".join(
        [figure.get("name", ""), figure.get("search", "")]
        + _flat(figure.get("maker")) + _flat(figure.get("version"))
        + _flat(figure.get("include_any")) + _flat(figure.get("must_include"))
    ))
    for w in OTHER_LINES:
        nw = normalise(w)
        if nw and nw in title and nw not in own:
            return f"other product line ({w})"
    for w in BOOTLEG_WORDS:
        if normalise(w) in title:
            return f"bootleg ({w})"
    kind = figure.get("type")
    if kind == "doll":
        for w in DOLL_ACCESSORY_WORDS:
            if normalise(w) in title:
                return f"doll accessory ({w})"
    if kind == "scale" and any(normalise(w) in title for w in KIT_WORDS) and "ガレージキット" not in own:
        return "garage kit"
    scale = figure.get("scale")
    if scale:
        scales = set(re.findall(r"1/\d+", title))
        if scales and normalise(scale) not in scales:
            return f"different scale ({', '.join(sorted(scales))})"
    own_makers = [normalise(m) for m in _flat(figure.get("maker"))]
    for m in KNOWN_MAKERS:
        nm = normalise(m)
        if nm in title and not any(nm in o or o in nm for o in own_makers):
            return f"different maker ({m})"
    # Outfit versions (swimsuit, bunny, maid...). For exact matches this only
    # applies when the figure lists its own version; the "possible match"
    # check always applies it.
    for group in VERSION_WORDS:
        hit = [w for w in group if normalise(w) in title]
        if hit and not any(normalise(w) in own for w in group):
            return f"different version ({hit[0]})" if figure.get("version") else f"maybe-version ({hit[0]})"
    return None


def match_level(item, figure, global_exclude, figure_words):
    """Returns "exact", "maybe" (right character + looks like a figure, but
    maker/version not confirmed), or None (not relevant)."""
    raw = strip_bonus(item["title"])
    title = normalise(raw)
    for word in list(figure.get("exclude") or []) + list(global_exclude or []):
        if normalise(word) and normalise(word) in title:
            return None
    max_price = figure.get("max_price")
    if max_price and item["price"] and item["price"] > int(max_price):
        return None
    why_not = contradiction(item, figure)
    if why_not and not why_not.startswith("maybe-"):
        return None
    if matches(item, figure, []):
        return "exact"
    if why_not:
        return None  # outfit version differs and nothing confirms it

    # --- "Possible match": right character and right kind of item ---
    character = figure.get("character")
    if character:
        char_ok = _has_each_group(title, _groups(character))
    else:
        must = figure.get("must_include")
        if must is None:
            must = (figure.get("search") or "").split()
        char_ok = bool(must) and all(normalise(w) in title for w in must)
    if not char_ok:
        return None
    kind = figure.get("type")
    type_words = TYPE_WORDS.get(kind, figure_words)
    if not any(normalise(w) in title for w in type_words):
        return None
    return "maybe"


def matches(item, figure, global_exclude):
    title = normalise(strip_bonus(item["title"]))
    for word in list(figure.get("exclude") or []) + list(global_exclude or []):
        if normalise(word) and normalise(word) in title:
            return False
    # must_include: every word must be in the title. If a figure doesn't say,
    # every word of its search must be in the title (sites like Rakuma pad
    # results with loose partial matches otherwise).
    must = figure.get("must_include")
    if must is None:
        must = (figure.get("search") or "").split()
    for word in must:
        if normalise(word) not in title:
            return False
    # include_any: at least one of these words. Can also be a list of lists,
    # meaning "one word from EACH group".
    any_of = figure.get("include_any") or []
    groups = any_of if any_of and isinstance(any_of[0], list) else ([any_of] if any_of else [])
    for group in groups:
        if not any(normalise(w) in title for w in group):
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

CURRENCY_SYMBOLS = {"GBP": "£", "USD": "US$", "CAD": "C$", "EUR": "€", "AUD": "A$"}


def get_rates(currencies):
    """Today's yen exchange rates, e.g. {"GBP": 0.0051, "USD": 0.0066}."""
    if not currencies:
        return {}
    wanted = ",".join(c.upper() for c in currencies)
    for url in (
        f"https://api.frankfurter.dev/v1/latest?base=JPY&symbols={wanted}",
        f"https://api.frankfurter.app/latest?from=JPY&to={wanted}",
    ):
        try:
            rates = requests.get(url, timeout=10).json()["rates"]
            return {c.upper(): float(rates[c.upper()]) for c in currencies if c.upper() in rates}
        except Exception:
            continue
    log("Couldn't get exchange rates this time - showing yen only.")
    return {}


def fmt_price(price, rates):
    if not price:
        return "price not shown"
    s = f"¥{price:,}"
    if rates:
        parts = [f"{CURRENCY_SYMBOLS.get(c, c + ' ')}{price * r:,.0f}" for c, r in rates.items()]
        s += " (≈ " + " · ".join(parts) + ")"
    return s


class Discord:
    def __init__(self, webhook, dry_run=False):
        self.webhook = webhook
        self.dry_run = dry_run

    def send(self, content=None, embeds=None, ping_everyone=False):
        payload = {"username": "Figure Watch"}
        if ping_everyone:
            content = "@everyone " + (content or "")
            payload["allowed_mentions"] = {"parse": ["everyone"]}
        else:
            payload["allowed_mentions"] = {"parse": []}
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


LABELS = {
    "new": "New listing",
    "back": "Back in stock",
    "drop": "Price drop",
    "listed": "For sale now",
}
MAYBE_COLOUR = 0xF1C40F  # yellow side bar for "possible match"

# A listing only counts as a possible match if its title has one of these,
# so cards, books, CDs etc. for the same character are still ignored.
DEFAULT_FIGURE_WORDS = [
    "フィギュア", "ガレージキット", "ガレキ", "キット", "レジン", "完成品", "塗装済",
    "スケール", "1/3", "1/4", "1/5", "1/6", "1/7", "1/8", "1/10", "1/12",
    "ドール", "DD", "ドルフィー", "ボークス", "VOLKS", "ワンフェス", "WF", "トレフェス",
    "ねんどろいど", "figma", "PVC", "バスト", "figure",
]

KIT_WORDS = ["ガレージキット", "ガレキ", "キット", "レジン", "未組立", "未塗装", "ワンフェス", "WF", "トレフェス"]
TYPE_WORDS = {
    "kit": KIT_WORDS,
    "doll": ["DD", "MDD", "ドルフィー", "ボークス", "VOLKS", "ドール"],
    "scale": ["フィギュア", "完成品", "塗装済", "スケール", "1/4", "1/5", "1/6", "1/7", "1/8", "PVC", "figure"],
}

# Other product lines - a "possible match" mentioning these is a different
# figure of the same character, so it's ignored.
OTHER_LINES = [
    "ねんどろいど", "ねんどろ", "figma", "プライズ", "一番くじ", "ぬいぐるみ",
    "ちびきゅん", "ぷちきゃら", "POP UP PARADE", "ポップアップパレード", "ちょこのせ",
    "デフォルメ", "SPM", "Coreful", "ルミナスタ", "ガチャ", "カプセル", "食玩",
    "ラバスト", "トレーディング", "ミニフィギュア", "BiCute", "ぬーどるストッパー",
    "アクリル", "ジオラマ", "ぴたぬい", "ぬいパル", "寝そべり", "ねそべり", "フィギュアーツ",
    "キューポッシュ", "Qposket", "ちびぐるみ", "ミニコレ", "ぺたん娘", "まめしき", "フィギュアライズ",
    "ガシャポン", "クレーンゲーム", "ちょこのっこ", "デスクトップキュート", "ぬーどる", "ひっかけ",
    "テニトル", "プーリップ", "ワーコレ", "WCF", "超合金", "マスコット",
]

# Figure makers - a "possible match" naming a maker that isn't this figure's
# maker is a different figure, so it's ignored.
KNOWN_MAKERS = [
    "グッドスマイル", "グッスマ", "GSC", "マックスファクトリー", "アルター", "ALTER",
    "コトブキヤ", "壽屋", "KOTOBUKIYA", "メガハウス", "バンダイ", "BANDAI", "フリュー",
    "FuRyu", "セガ", "SEGA", "タイトー", "TAITO", "FREEing", "フリーイング",
    "ネイティブ", "Native", "ミユキ", "BINDing", "バインディング", "ボークス", "VOLKS",
    "ホビージャパン", "アクアマリン", "キューズQ", "オーキッドシード", "ダイキ工業",
    "Pink Charm", "ピンクチャーム", "F.W.A.T", "FWAT", "Myethos", "ユニオンクリエイティブ",
    "SkyTube", "スカイチューブ", "Lechery", "AniGame", "ムービック", "アニプレックス",
    "ファニーナイツ", "ウイング", "WINGS", "あみあみ", "AMAKUNI", "プラム", "PLUM",
    "ケイズファクトリー", "ベルファイン", "ヴェルテクス", "Vertex", "ensoutoys",
    "ホットトイズ", "海洋堂", "エンブレイスジャパン", "オルカトイズ", "アオシマ",
]

# Outfit versions: a listing naming one of these is a different version of
# the character unless the figure itself is that version.
VERSION_WORDS = [
    ["水着", "スイムスーツ"], ["ビキニ"], ["バニー"], ["チャイナドレス", "チャイナ服"],
    ["ネグリジェ"], ["メイド"], ["浴衣"], ["着物", "振袖"], ["ナース"], ["サンタ"],
    ["ウェディング", "花嫁"], ["体操服", "ブルマ"], ["スクール水着", "スク水"],
]

# Clear signs of an unofficial copy.
BOOTLEG_WORDS = ["中国版", "CN版", "CNver", "Chinaver", "海外版", "非正規", "海賊版", "互換品",
                 "コピー品", "レプリカ", "複製品", "パチモン"]

# Doll outfits/parts sold on their own (for Dollfie Dream watches).
DOLL_ACCESSORY_WORDS = ["衣装", "制服セット", "アウトフィット", "ウィッグ", "ドール用", "DD用", "DDdy用",
                        "DDS用", "DDP用", "MDD用", "SD用", "ドレスセット", "シューズ", "靴",
                        "ドールアイ", "アイのみ", "ヘッドのみ", "ボディのみ", "カスタムヘッド",
                        "メイク済みヘッド", "コスプレ", "ハンガー", "スタンド単品"]

# On the very first check, show at most this many current listings per site.
MAX_FIRST_RUN_PER_SITE = 20


def build_embed(kind, item, site, old_price, rates):
    price_line = fmt_price(item["price"], rates)
    if kind == "drop":
        price_line = f"~~¥{old_price:,}~~ → {price_line}"
    maybe = item.get("maybe")
    description = f"**{price_line}**"
    if maybe:
        description = "⚠️ **Warning: this may be inaccurate**\n" + description
    embed = {
        "title": (("⚠️ " if maybe else "") + (item["title"] or "(no title)"))[:250],
        "url": item["url"],
        "description": description,
        "color": MAYBE_COLOUR if maybe else SITE_COLOURS[site],
        "footer": {"text": f"{SITE_NAMES[site]} · {LABELS[kind]}" + (" · possible match" if maybe else "")},
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


def run(dry_run=False, show_all=False):
    cfg, figures = load_config()
    settings = cfg.get("settings") or {}
    default_sites = settings.get("sites") or list(SITE_NAMES)
    global_exclude = settings.get("exclude") or []
    figure_words = settings.get("figure_words") or DEFAULT_FIGURE_WORDS
    ping_mode = str(settings.get("ping_everyone", "exact")).lower()
    pause = float(settings.get("pause_seconds", 3))

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook and not dry_run:
        raise SystemExit("DISCORD_WEBHOOK_URL is not set (add it as a GitHub secret).")
    discord = Discord(webhook, dry_run=dry_run)
    currencies = settings.get("currencies")
    if currencies is None:
        currencies = ["GBP"] if settings.get("show_gbp", True) else []
    rates = get_rates(currencies)

    state = load_state()
    ts = now_ts()
    session = requests.Session()
    mercari = Mercari()
    failed_sites = {}   # site -> last error message
    site_errors = {}    # site -> number of failed searches this run
    site_ok = set()     # sites that answered at least once this run
    given_up = set()
    summary = []
    fig = {}

    def search_site(site, query):
        kind = fig.get("type")
        if site == "mercari":
            return mercari.search(session, query, kind)
        if site == "yahoo":
            return search_yahoo(session, query, kind)
        if site == "surugaya":
            return search_surugaya(session, query)
        if site == "yahooflea":
            return search_yahoo_flea(session, query)
        if site == "rakuma":
            return search_rakuma(session, query)
        if site == "amiami":
            return search_amiami(session, query, kind)
        if site == "mandarake":
            return search_mandarake(session, query, kind)
        return search_rakuten(session, query, fig.get("max_price"))

    for fig in figures:
        name = fig["name"]
        sites = fig.get("sites") or default_sites
        fig_embeds = []
        maybe_embeds = []
        first_embeds = []
        baseline_counts = []

        for site in sites:
            if site not in SITE_NAMES:
                log(f"Unknown site '{site}' for {name} - skipping")
                continue
            if site in given_up:
                continue  # don't keep hammering a site that keeps refusing
            # JAN barcodes work well on shops; marketplaces need words.
            query = fig.get("search")
            if fig.get("jan") and (site in ("surugaya", "rakuten") or not query):
                query = str(fig["jan"])
            found, error = None, None
            for attempt in range(2):  # one retry after a short wait
                try:
                    found = search_site(site, query)
                    error = None
                    break
                except Exception as e:  # noqa: BLE001 - one bad site shouldn't stop the rest
                    error = f"{type(e).__name__}: {e}"
                    log(f"  ! {SITE_NAMES[site]} failed ({name}, try {attempt + 1}): {error[:300]}")
                    time.sleep(RETRY_WAIT)
            time.sleep(pause)
            if error:
                failed_sites[site] = error
                site_errors[site] = site_errors.get(site, 0) + 1
                if site_errors[site] >= GIVE_UP_AFTER:
                    given_up.add(site)
                    log(f"  ! Giving up on {SITE_NAMES[site]} for this run.")
                continue
            site_ok.add(site)
            if found is None:
                continue  # Rakuten without keys

            # When searching by JAN the title won't contain the words, so
            # only apply price / exclude filters in that case.
            fig_for_match = dict(fig)
            if query == str(fig.get("jan")):
                fig_for_match["must_include"] = []
                fig_for_match["include_any"] = []
                # Rakuten's keyword search is loose even for barcodes, so
                # there the listing must still name the character.
                if site == "rakuten":
                    key_words = fig.get("must_include") or (fig.get("search") or "").split()
                    found = [
                        i for i in found
                        if any(normalise(w) in normalise(i["title"]) for w in key_words)
                    ]
            good = []
            for i in found:
                level = match_level(i, fig_for_match, global_exclude, figure_words)
                if level:
                    i["maybe"] = level == "maybe"
                    good.append(i)
            n_maybe = sum(1 for i in good if i["maybe"])
            log(f"{name} @ {SITE_NAMES[site]}: {len(found)} results, "
                f"{len(good) - n_maybe} match, {n_maybe} possible")
            for i in good:
                price = f"¥{i['price']:,}" if i["price"] else "¥?"
                tag = "?" if i["maybe"] else " "
                log(f"  {tag} {price:>10}  {i['title'][:60]}  {i['url']}")
            summary.append((name, site, len(found), len(good)))

            if show_all:
                # Everything for sale now; the bot's memory is not touched.
                alerts, baselining = [], True
            else:
                alerts, baselining = diff_results(state, name, site, good, ts)
            if baselining:
                # First time for this figure+site: show everything already for
                # sale (cheapest first), each with its own link.
                listed = sorted(good, key=lambda i: (i["maybe"], i["price"] or 10**12))
                baseline_counts.append(f"{SITE_NAMES[site]}: {len(good)}")
                for item in listed[:MAX_FIRST_RUN_PER_SITE]:
                    first_embeds.append(build_embed("listed", item, site, None, rates))
                if len(listed) > MAX_FIRST_RUN_PER_SITE:
                    baseline_counts[-1] += f" (showing cheapest {MAX_FIRST_RUN_PER_SITE})"
            for kind, item, old in alerts:
                embed = build_embed(kind, item, site, old, rates)
                if item.get("maybe"):
                    maybe_embeds.append((kind, embed))
                else:
                    fig_embeds.append((kind, embed))

        if baseline_counts and show_all:
            discord.send(
                f"👀🎀 **{name}** – everything for sale right now: "
                + " · ".join(baseline_counts)
                + ("" if first_embeds else "\nNothing for sale at the moment.")
            )
            for i in range(0, len(first_embeds), 10):
                discord.send(embeds=first_embeds[i:i + 10])
        elif baseline_counts:
            discord.send(
                f"👀🎀 Now watching **{name}**. For sale right now: "
                + " · ".join(baseline_counts)
                + ("" if first_embeds else "\nNothing listed yet. You'll be pinged when something appears.")
            )
            for i in range(0, len(first_embeds), 10):
                discord.send(embeds=first_embeds[i:i + 10])
        # Only brand-new listings ping @everyone; restocks and price drops
        # are posted quietly.
        groups = [
            ([e for k, e in fig_embeds if k == "new"], f"👀🎀 **{name}**",
             ping_mode in ("exact", "all")),
            ([e for k, e in fig_embeds if k != "new"], f"👀🎀 **{name}**", False),
            ([e for k, e in maybe_embeds if k == "new"],
             f"👀🎀 **{name}** – ⚠️ possible match, please check", ping_mode == "all"),
            ([e for k, e in maybe_embeds if k != "new"],
             f"👀🎀 **{name}** – ⚠️ possible match, please check", False),
        ]
        for embeds, content, ping in groups:
            for i in range(0, len(embeds), 10):
                discord.send(content=content, embeds=embeds[i:i + 10],
                             ping_everyone=ping and i == 0)

    # Failure tracking: warn once if a site keeps failing.
    for site in ([] if show_all else SITE_NAMES):
        if site in failed_sites and site not in site_ok:
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

    if show_all and failed_sites:
        discord.send("ℹ️ Some searches failed on: " + ", ".join(SITE_NAMES[s] for s in failed_sites)
                     + "\n" + "\n".join(f"• {SITE_NAMES[s]}: `{e[:150]}`" for s, e in failed_sites.items()))
    if not dry_run and not show_all:
        save_state(state)
    log(f"Done. {len(summary)} searches, {len(failed_sites)} site(s) failed.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--test-discord", action="store_true")
    p.add_argument("--mode", choices=["normal", "show-all", "dry-run", "test-discord"], default=None)
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
    run(dry_run=(mode == "dry-run"), show_all=(mode == "show-all"))


if __name__ == "__main__":
    sys.exit(main())

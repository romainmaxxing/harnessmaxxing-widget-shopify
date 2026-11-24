"""Shopify stats - HTML widget for Harness Maxxing (read only).

The widget (widget.html) draws everything client side from two calls, both read only:

    GET stats          {ok, lang, demo, data: {shop, tz, source, visitors, today, hour,
                        series: {since, s[], o[], v[]|null}   one value per day, since -> today (about 2 years)
                        hours: {date: {s[24], o[24], v[24]|null}}  today and the 2 days before, shop time
                        last[], at}}
    GET top?from=YYYY-MM-DD&to=YYYY-MM-DD    {ok, data: {top[], countries[], partial}}  for any period

s = sales, o = orders, v = online store visitors. Periods, comparisons, weeks and ratios are computed by the widget.

Keys (Settings -> Keys), first match wins:
    store   SHOPIFY_STORE or SHOPIFY_SHOP              myshop.myshopify.com
    token   SHOPIFY_ADMIN_TOKEN or SHOPIFY_ADMIN_ACCESS_TOKEN   shpat_...
    or      SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET  (client_credentials grant, token cached 24 h)
Scopes: read_orders (required), read_reports (ShopifyQL: net sales, visitors, countries, 2 years of history).
Without ShopifyQL the widget falls back to the orders of the last FALLBACK_DAYS days (gross totals).

Stdlib only, every request has a timeout, results are cached CACHE_S seconds in state_dir.
"""
import json
import os
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API_VERSION = "2025-10"
CACHE_S = 300          # normal cache
CLOSED_CACHE_S = 3600  # top lists of a period that is over
FORCE_MIN_S = 30       # a forced refresh never hits Shopify more often than this
DEADLINE_S = 9         # total budget of one refresh
SERIES_DAYS = 730      # 12 months + the 12 before (and always since January 1 of last year)
HOUR_DAYS = 3          # today, yesterday and the day before, hour by hour
FALLBACK_DAYS = 62     # history read from the orders when ShopifyQL is not allowed
MAX_RANGE_DAYS = 800
UA = "harness-maxxing-shopify-stats/1.1"


class ShopError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


# ----------------------------------------------------------------------------- keys and token

def _secret(ctx, *names):
    for n in names:
        v = ctx["secret"](n)
        if v and v.strip():
            return v.strip()
    return None


def _store(ctx):
    s = _secret(ctx, "SHOPIFY_STORE", "SHOPIFY_SHOP")
    if not s:
        return None
    s = s.lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    return s if "." in s else f"{s}.myshopify.com"


def _write_private(path, obj):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    tmp.replace(path)


def _grant(ctx, store, deadline):
    """Token from the client_credentials grant (custom apps of the Dev Dashboard), cached in state_dir."""
    cid, sec = _secret(ctx, "SHOPIFY_CLIENT_ID"), _secret(ctx, "SHOPIFY_CLIENT_SECRET")
    if not (cid and sec):
        return None
    req = urllib.request.Request(
        f"https://{store}/admin/oauth/access_token",
        data=json.dumps({"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=_left(deadline)) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise ShopError("auth", f"Shopify refused the app credentials (HTTP {e.code})") from None
    except (OSError, ValueError) as e:
        raise ShopError("network", f"Shopify unreachable ({type(e).__name__})") from None
    _write_private(Path(ctx["state_dir"]) / "token.json",
                   {"store": store, "token": d["access_token"], "exp": time.time() + int(d.get("expires_in", 86400))})
    return d["access_token"]


def _cached_grant(ctx, store):
    try:
        c = json.loads((Path(ctx["state_dir"]) / "token.json").read_text())
        if c["store"] == store and c["exp"] > time.time() + 300:
            return c["token"]
    except (OSError, ValueError, KeyError):
        pass
    return None


# ----------------------------------------------------------------------------- GraphQL

def _left(deadline):
    left = deadline - time.time()
    if left <= 0.5:
        raise ShopError("timeout", "Shopify is too slow to answer")
    return min(8.0, left)


class Client:
    def __init__(self, ctx, store, deadline):
        self.ctx, self.store, self.deadline = ctx, store, deadline
        self.token = _cached_grant(ctx, store) or _secret(ctx, "SHOPIFY_ADMIN_TOKEN", "SHOPIFY_ADMIN_ACCESS_TOKEN")
        self.renewed = False
        if not self.token:
            self.token = _grant(ctx, store, deadline)
            self.renewed = True
        if not self.token:
            raise ShopError("missing_keys")

    def gql(self, query, variables=None):
        try:
            return self._post(query, variables)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and not self.renewed:
                self.renewed = True
                tok = _grant(self.ctx, self.store, self.deadline)
                if tok:
                    self.token = tok
                    return self._post(query, variables)
            if e.code in (401, 403):
                raise ShopError("auth", f"Shopify refused the token (HTTP {e.code})") from None
            raise ShopError("http", f"Shopify answered HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise ShopError("network", f"Shopify unreachable ({e.reason})") from None
        except (TimeoutError, OSError, ValueError) as e:
            raise ShopError("network", f"Shopify unreachable ({type(e).__name__})") from None

    def _post(self, query, variables):
        req = urllib.request.Request(
            f"https://{self.store}/admin/api/{API_VERSION}/graphql.json",
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={"X-Shopify-Access-Token": self.token, "Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=_left(self.deadline)) as r:
            d = json.load(r)
        if d.get("errors"):
            msg = "; ".join(str(x.get("message", x)) for x in d["errors"])[:200]
            raise ShopError("access" if "access" in msg.lower() else "query", msg)
        return d["data"]

    def ql(self, q):
        res = self.gql(QL, {"q": q})["shopifyqlQuery"]
        if res.get("parseErrors"):
            raise ShopError("query", str(res["parseErrors"])[:200])
        return (res.get("tableData") or {}).get("rows") or []


SHOP_Q = "{ shop { name currencyCode ianaTimezone myshopifyDomain primaryDomain { url } } }"
QL = "query($q: String!) { shopifyqlQuery(query: $q) { tableData { rows } parseErrors } }"
LAST_Q = """{ orders(first: 6, reverse: true, sortKey: CREATED_AT) { nodes {
  name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus
  totalPriceSet { shopMoney { amount } } } } }"""
ORDERS_Q = """query($q: String!, $after: String) {
  orders(first: 50, after: $after, sortKey: CREATED_AT, query: $q) {
    pageInfo { hasNextPage endCursor }
    nodes { createdAt cancelledAt test totalPriceSet { shopMoney { amount } }
      lineItems(first: 10) { nodes { title quantity originalTotalSet { shopMoney { amount } } } } } } }"""


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0



# ----------------------------------------------------------------------------- shapes

def _series_start(today):
    return min(date(today.year - 1, 1, 1), today - timedelta(days=SERIES_DAYS - 1))


def _empty_series(since, today, visitors):
    n = (today - since).days + 1
    return {"since": since.isoformat(), "s": [0.0] * n, "o": [0] * n, "v": [0] * n if visitors else None}


def _empty_hours(today, visitors):
    return {(today - timedelta(days=i)).isoformat(): {"s": [0.0] * 24, "o": [0] * 24,
                                                      "v": [0] * 24 if visitors else None}
            for i in range(HOUR_DAYS)}


def _put(series, day, key, value):
    i = (date.fromisoformat(day) - date.fromisoformat(series["since"])).days
    if 0 <= i < len(series[key]):
        series[key][i] += value


def _local(iso, tz):
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
    return t.date().isoformat(), t.hour


def _round(series):
    series["s"] = [round(x, 2) for x in series["s"]]
    return series


def _parallel(c, queries):
    """{name: ShopifyQL} -> {name: rows or None}; an optional query that fails does not break the widget."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {k: pool.submit(c.ql, q) for k, q in queries.items()}
        out = {}
        for k, f in futures.items():
            try:
                out[k] = f.result()
            except ShopError:
                out[k] = None
    return out


# ----------------------------------------------------------------------------- ShopifyQL source

def _from_shopifyql(c, tz, today):
    since = _series_start(today)
    span = f"SINCE {since.isoformat()} UNTIL {today.isoformat()}"
    hspan = f"SINCE {(today - timedelta(days=HOUR_DAYS - 1)).isoformat()} UNTIL {today.isoformat()}"
    # Sales first and alone: if ShopifyQL is not allowed for this token, nothing else is sent.
    sales = c.ql(f"FROM sales SHOW total_sales, orders {span} TIMESERIES day")
    res = _parallel(c, {"visits": f"FROM sessions SHOW online_store_visitors {span} TIMESERIES day",
                        "hsales": f"FROM sales SHOW total_sales, orders {hspan} TIMESERIES hour",
                        "hvisits": f"FROM sessions SHOW online_store_visitors {hspan} TIMESERIES hour"})
    visitors = res["visits"] is not None
    series = _empty_series(since, today, visitors)
    for r in sales:
        _put(series, r["day"], "s", _num(r.get("total_sales")))
        _put(series, r["day"], "o", int(_num(r.get("orders"))))
    for r in res["visits"] or []:
        _put(series, r["day"], "v", int(_num(r.get("online_store_visitors"))))
    hours = _empty_hours(today, visitors and res["hvisits"] is not None)
    for r in res["hsales"] or []:
        d, h = _local(r["hour"], tz)
        if d in hours:
            hours[d]["s"][h] = round(hours[d]["s"][h] + _num(r.get("total_sales")), 2)
            hours[d]["o"][h] += int(_num(r.get("orders")))
    for r in res["hvisits"] or []:
        d, h = _local(r["hour"], tz)
        if d in hours and hours[d]["v"] is not None:
            hours[d]["v"][h] += int(_num(r.get("online_store_visitors")))
    return _round(series), hours, visitors


def _top_shopifyql(c, a, b):
    span = f"SINCE {a} UNTIL {b}"
    res = _parallel(c, {
        "top": f"FROM sales SHOW total_sales, net_items_sold GROUP BY product_title {span} "
               f"ORDER BY total_sales DESC LIMIT 8",
        "geo": f"FROM sales SHOW total_sales GROUP BY billing_country {span} ORDER BY total_sales DESC LIMIT 6"})
    if res["top"] is None and res["geo"] is None:
        raise ShopError("query", "ShopifyQL refused the top lists")
    top = [{"t": r["product_title"], "s": round(_num(r.get("total_sales")), 2), "q": int(_num(r.get("net_items_sold")))}
           for r in res["top"] or [] if r.get("product_title") and _num(r.get("total_sales")) > 0][:5]
    geo = [{"c": r["billing_country"], "s": round(_num(r.get("total_sales")), 2)}
           for r in res["geo"] or [] if r.get("billing_country") and _num(r.get("total_sales")) > 0][:5]
    return {"top": top, "countries": geo, "partial": False}


# ----------------------------------------------------------------------------- orders source (fallback)

def _orders(c, start, end, tz):
    """Orders created between two local dates (included), without the cancelled and test ones (1000 at most)."""
    q = f"created_at:>={start.isoformat()} created_at:<{(end + timedelta(days=1)).isoformat()}"
    out, after, truncated = [], None, False
    for page in range(20):
        d = c.gql(ORDERS_Q, {"q": q, "after": after})["orders"]
        for o in d["nodes"]:
            if o.get("cancelledAt") or o.get("test"):
                continue
            t = datetime.fromisoformat(o["createdAt"].replace("Z", "+00:00")).astimezone(tz)
            if start <= t.date() <= end:
                out.append((t, _num(o["totalPriceSet"]["shopMoney"]["amount"]), o["lineItems"]["nodes"]))
        if not d["pageInfo"]["hasNextPage"]:
            break
        after = d["pageInfo"]["endCursor"]
        if page == 19 or time.time() > c.deadline - 2:
            truncated = True
            break
    return out, truncated


def _from_orders(c, tz, today):
    since = today - timedelta(days=FALLBACK_DAYS - 1)
    orders, truncated = _orders(c, since, today, tz)
    series, hours = _empty_series(since, today, False), _empty_hours(today, False)
    for t, total, _ in orders:
        d = t.date().isoformat()
        _put(series, d, "s", total)
        _put(series, d, "o", 1)
        if d in hours:
            hours[d]["s"][t.hour] = round(hours[d]["s"][t.hour] + total, 2)
            hours[d]["o"][t.hour] += 1
    return _round(series), hours, truncated


def _top_orders(c, tz, a, b):
    today = datetime.now(tz).date()
    start = max(a, today - timedelta(days=FALLBACK_DAYS - 1))
    if start > b:
        return {"top": [], "countries": [], "partial": True}
    orders, truncated = _orders(c, start, b, tz)
    agg = {}
    for _, _, items in orders:
        for li in items:
            x = agg.setdefault(li["title"], {"t": li["title"], "s": 0.0, "q": 0})
            x["s"] += _num(li["originalTotalSet"]["shopMoney"]["amount"])
            x["q"] += int(li.get("quantity") or 0)
    top = sorted(agg.values(), key=lambda x: -x["s"])[:5]
    return {"top": [{**x, "s": round(x["s"], 2)} for x in top], "countries": [],
            "partial": truncated or start > a}


# ----------------------------------------------------------------------------- refresh

def _tzinfo(name):
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return timezone.utc


def _fetch(ctx, store):
    c = Client(ctx, store, time.time() + DEADLINE_S)
    shop = c.gql(SHOP_Q)["shop"]
    tz = _tzinfo(shop.get("ianaTimezone"))
    now = datetime.now(tz)
    truncated = False
    try:
        series, hours, visitors = _from_shopifyql(c, tz, now.date())
        source = "shopifyql"
    except ShopError as e:
        if e.code in ("auth", "network", "timeout"):
            raise
        series, hours, truncated = _from_orders(c, tz, now.date())
        visitors, source = False, "orders"
    try:
        last = [{"n": o["name"], "at": o["createdAt"], "total": round(_num(o["totalPriceSet"]["shopMoney"]["amount"]), 2),
                 "pay": (o.get("displayFinancialStatus") or "").lower(),
                 "ship": (o.get("displayFulfillmentStatus") or "").lower(), "canceled": bool(o.get("cancelledAt"))}
                for o in c.gql(LAST_Q)["orders"]["nodes"]]
    except ShopError:
        last = []
    handle = (shop.get("myshopifyDomain") or store).split(".")[0]
    return {"shop": {"name": shop.get("name") or handle, "currency": shop.get("currencyCode") or "USD"},
            "tz": str(tz), "source": source, "truncated": truncated, "visitors": bool(visitors),
            "today": now.date().isoformat(), "hour": now.hour, "series": series, "hours": hours,
            "last": last, "at": int(time.time())}


# ----------------------------------------------------------------------------- demo data

DEMO_PRODUCTS = ["Affiche Montagne Brume", "Poster Vague Indigo", "Cadre chêne 50x70", "Affiche Tokyo Nuit",
                 "Poster Botanique", "Affiche Désert Rose", "Poster Lune Minimal", "Affiche Phare Breton"]
DEMO_COUNTRIES = ["France", "Germany", "Belgium", "Switzerland", "Canada", "Spain"]
# share of a day's traffic per hour (sums to 1): quiet nights, a lunch bump, a peak around 21 h
_PROFILE = [0.8, 0.5, 0.3, 0.2, 0.2, 0.3, 0.6, 1.2, 2.0, 2.6, 3.0, 3.4,
            4.2, 4.4, 3.8, 3.6, 3.8, 4.2, 4.8, 5.6, 6.4, 6.8, 5.4, 2.8]
PROFILE = [x / sum(_PROFILE) for x in _PROFILE]


def _demo(ctx):
    """Plausible, made up figures (same on every call of a day) for previews and screenshots."""
    tz = _tzinfo(ctx["config"].get("timezone"))
    now = datetime.now(tz)
    today = now.date()
    since = _series_start(today)
    series = _empty_series(since, today, True)
    rnd = random.Random(2026)
    n = len(series["s"])
    for i in range(n):
        d = since + timedelta(days=i)
        growth = 0.7 + 0.6 * i / n
        season = {11: 1.3, 12: 1.45, 1: 0.9, 7: 0.85, 8: 0.8}.get(d.month, 1.0)
        week = 1.15 if d.weekday() in (5, 6) else 1.0
        v = int(max(200, rnd.gauss(1300, 150)) * growth * season * week)
        o = max(1, round(v * rnd.uniform(0.011, 0.018) * (1.1 if d.month in (11, 12) else 1.0)))
        series["v"][i], series["o"][i], series["s"][i] = v, o, round(o * rnd.uniform(39, 51), 2)
    hours = _empty_hours(today, True)
    for key, h in hours.items():
        i = (date.fromisoformat(key) - since).days
        last = now.hour if key == today.isoformat() else 23
        for x in ("s", "o", "v"):
            total = series[x][i]
            vals = [total * PROFILE[k] * rnd.uniform(0.7, 1.3) if k <= last else 0 for k in range(24)]
            h[x] = [round(y, 2) for y in vals] if x == "s" else [round(y) for y in vals]
            series[x][i] = round(sum(h[x]), 2) if x == "s" else sum(h[x])
    pays = ["paid", "paid", "paid", "pending", "paid", "refunded"]
    ships = ["unfulfilled", "unfulfilled", "fulfilled", "unfulfilled", "fulfilled", "fulfilled"]
    last, t = [], datetime.now(timezone.utc)
    for i in range(6):
        t -= timedelta(minutes=rnd.randint(12, 140))
        last.append({"n": f"#{4821 - i}", "at": t.isoformat().replace("+00:00", "Z"),
                     "total": round(rnd.uniform(29, 129), 2), "pay": pays[i], "ship": ships[i], "canceled": False})
    return {"shop": {"name": "Atelier Nord", "currency": "EUR"}, "tz": str(tz), "source": "demo", "truncated": False,
            "visitors": True, "today": today.isoformat(), "hour": now.hour, "series": series, "hours": hours,
            "last": last, "at": int(time.time())}


def _demo_top(ctx, a, b):
    data = _demo(ctx)
    since = date.fromisoformat(data["series"]["since"])
    total = sum(data["series"]["s"][max(0, (a - since).days):(b - since).days + 1])
    rnd = random.Random(f"{a}{b}")
    weights = sorted((rnd.uniform(0.35, 1.0) for _ in DEMO_PRODUCTS), reverse=True)
    names = rnd.sample(DEMO_PRODUCTS, len(DEMO_PRODUCTS))
    k = 0.75 * total / sum(weights)
    top = [{"t": n, "s": round(w * k, 2), "q": max(1, round(w * k / 42))}
           for n, w in zip(names, weights, strict=True)][:5]
    shares = [0.46, 0.18, 0.12, 0.09, 0.06]
    return {"top": top, "countries": [{"c": c, "s": round(total * s, 2)}
                                      for c, s in zip(DEMO_COUNTRIES, shares, strict=False)], "partial": False}


# ----------------------------------------------------------------------------- entry point

def _lang(ctx):
    return "fr" if str(ctx["config"].get("language") or "").startswith("fr") else "en"


def _range(query):
    try:
        a, b = date.fromisoformat(str(query.get("from"))), date.fromisoformat(str(query.get("to")))
    except ValueError:
        return None
    return (a, b) if a <= b and (b - a).days <= MAX_RANGE_DAYS else None


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _stats(ctx, store, force):
    cache = Path(ctx["state_dir"]) / "stats.json"
    c = _read(cache)
    if c and c.get("store") == store and c.get("v") == 2:
        age = time.time() - c["at"]
        if age < FORCE_MIN_S or (age < CACHE_S and not force):
            return {"ok": True, "data": c["data"]}
    else:
        c = None
    try:
        data = _fetch(ctx, store)
    except (ShopError, KeyError, TypeError) as e:   # Key/TypeError: an answer of an unexpected shape
        code = e.code if isinstance(e, ShopError) else "failed"
        return {"ok": bool(c), "data": c["data"] if c else None, "error": code, "message": str(e)[:200]}
    _write_private(cache, {"v": 2, "at": time.time(), "store": store, "data": data})
    return {"ok": True, "data": data}


def _top(ctx, store, a, b, force):
    path = Path(ctx["state_dir"]) / "top.json"
    cache = _read(path) or {}
    key = f"{store}|{a}|{b}"
    stats = (_read(Path(ctx["state_dir"]) / "stats.json") or {}).get("data") or {}
    tz = _tzinfo(stats.get("tz"))
    ttl = CLOSED_CACHE_S if b < datetime.now(tz).date() else CACHE_S
    hit = cache.get(key)
    if hit and (time.time() - hit["at"] < FORCE_MIN_S or (time.time() - hit["at"] < ttl and not force)):
        return {"ok": True, "data": hit["data"]}
    try:
        c = Client(ctx, store, time.time() + DEADLINE_S)
        if stats.get("source") == "orders":
            data = _top_orders(c, tz, a, b)
        else:
            try:
                data = _top_shopifyql(c, a.isoformat(), b.isoformat())
            except ShopError as e:
                if e.code in ("auth", "network", "timeout"):
                    raise
                data = _top_orders(c, tz, a, b)
    except (ShopError, KeyError, TypeError) as e:
        code = e.code if isinstance(e, ShopError) else "failed"
        return {"ok": bool(hit), "data": hit["data"] if hit else None, "error": code, "message": str(e)[:200]}
    cache[key] = {"at": time.time(), "data": data}
    cache = dict(sorted(cache.items(), key=lambda kv: kv[1]["at"])[-40:])
    _write_private(path, cache)
    return {"ok": True, "data": data}


def handle(ctx, method, path, query, body):
    query = query or {}
    if method != "GET" or path not in ("stats", "top"):
        return 404, {"error": "not_found", "message": f"{method} {path}"}
    lang = _lang(ctx)
    demo = str(query.get("demo", "")) == "1" or _secret(ctx, "SHOPIFY_STATS_DEMO") == "1"
    force = str(query.get("force", "")) == "1"
    rng = None
    if path == "top":
        rng = _range(query)
        if not rng:
            return 400, {"error": "bad_range", "message": "from and to: YYYY-MM-DD, from <= to"}
    if demo:
        data = _demo(ctx) if path == "stats" else _demo_top(ctx, *rng)
        return {"ok": True, "lang": lang, "demo": True, "data": data}
    store = _store(ctx)
    if not store:
        return {"ok": False, "lang": lang, "demo": False, "error": "missing_keys"}
    res = _stats(ctx, store, force) if path == "stats" else _top(ctx, store, *rng, force)
    return {"lang": lang, "demo": False, **res}

"""Shopify stats - HTML widget for Harness Maxxing (read only).

The widget (widget.html) calls GET `stats` and draws everything client side. This file only reads the
store through the Shopify Admin GraphQL API and returns one JSON document:

    {ok, lang, demo, data: {shop, source, days[60], periods{1,7,30}: {top, countries}, last, visitors}}

Keys (Settings -> Keys), first match wins:
    store   SHOPIFY_STORE or SHOPIFY_SHOP              myshop.myshopify.com
    token   SHOPIFY_ADMIN_TOKEN or SHOPIFY_ADMIN_ACCESS_TOKEN   shpat_...
    or      SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET  (client_credentials grant, token cached 24 h)
Scopes: read_orders (required), read_reports / read_analytics (ShopifyQL: net sales, visitors, countries).
Without ShopifyQL the widget falls back to the orders of the last 60 days.

Stdlib only, every request has a timeout, results are cached CACHE_S seconds in state_dir.
"""
import json
import os
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API_VERSION = "2025-10"
CACHE_S = 300          # normal cache
FORCE_MIN_S = 30       # a forced refresh never hits Shopify more often than this
DEADLINE_S = 9         # total budget of one refresh
DAYS = 60              # 30 days shown + the 30 before, for the comparison
PERIODS = (1, 7, 30)
UA = "harness-maxxing-shopify-stats/1.0"


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


# ----------------------------------------------------------------------------- data sources

def _span(days):
    return f"SINCE -{days - 1}d UNTIL today"


def _from_shopifyql(c):
    """Net sales, orders and visitors per day + top products and countries per period."""
    queries = {"sales": f"FROM sales SHOW total_sales, orders {_span(DAYS)} TIMESERIES day",
               "visits": f"FROM sessions SHOW online_store_visitors {_span(DAYS)} TIMESERIES day"}
    for p in PERIODS:
        queries[f"top{p}"] = (f"FROM sales SHOW total_sales, net_items_sold GROUP BY product_title {_span(p)} "
                              f"ORDER BY total_sales DESC LIMIT 8")
        queries[f"geo{p}"] = (f"FROM sales SHOW total_sales GROUP BY billing_country {_span(p)} "
                              f"ORDER BY total_sales DESC LIMIT 6")
    # Sales first and alone: if ShopifyQL is not allowed for this token, nothing else is sent.
    sales = c.ql(queries.pop("sales"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {k: pool.submit(c.ql, q) for k, q in queries.items()}
        res = {}
        for k, f in futures.items():
            try:
                res[k] = f.result()
            except ShopError:
                res[k] = None           # one optional query failing does not break the widget
    per = {}
    for r in sales:
        per[r["day"]] = {"s": round(_num(r.get("total_sales")), 2), "o": int(_num(r.get("orders"))), "v": None}
    visitors = res["visits"] is not None
    for r in res["visits"] or []:
        per.setdefault(r["day"], {"s": 0, "o": 0})["v"] = int(_num(r.get("online_store_visitors")))
    periods = {}
    for p in PERIODS:
        top = [{"t": r["product_title"], "s": round(_num(r.get("total_sales")), 2), "q": int(_num(r.get("net_items_sold")))}
               for r in res[f"top{p}"] or [] if r.get("product_title") and _num(r.get("total_sales")) > 0][:5]
        geo = [{"c": r["billing_country"], "s": round(_num(r.get("total_sales")), 2)}
               for r in res[f"geo{p}"] or [] if r.get("billing_country") and _num(r.get("total_sales")) > 0][:5]
        periods[str(p)] = {"top": top, "countries": geo}
    return per, periods, visitors


def _from_orders(c, tz):
    """Fallback without ShopifyQL: gross totals of the orders of the last DAYS days (at most 1000 orders)."""
    start = (datetime.now(tz) - timedelta(days=DAYS - 1)).date()
    per, items, after, truncated = {}, [], None, False
    for page in range(20):
        d = c.gql(ORDERS_Q, {"q": f"created_at:>={start.isoformat()}", "after": after})["orders"]
        for o in d["nodes"]:
            if o.get("cancelledAt") or o.get("test"):
                continue
            day = datetime.fromisoformat(o["createdAt"].replace("Z", "+00:00")).astimezone(tz).date().isoformat()
            x = per.setdefault(day, {"s": 0.0, "o": 0, "v": None})
            x["s"] = round(x["s"] + _num(o["totalPriceSet"]["shopMoney"]["amount"]), 2)
            x["o"] += 1
            for li in o["lineItems"]["nodes"]:
                items.append((day, li["title"], int(li.get("quantity") or 0),
                              _num(li["originalTotalSet"]["shopMoney"]["amount"])))
        if not d["pageInfo"]["hasNextPage"]:
            break
        after = d["pageInfo"]["endCursor"]
        if page == 19 or time.time() > c.deadline - 2:
            truncated = True
            break
    today = datetime.now(tz).date()
    periods = {}
    for p in PERIODS:
        since = (today - timedelta(days=p - 1)).isoformat()
        agg = {}
        for day, title, q, s in items:
            if day >= since:
                a = agg.setdefault(title, {"t": title, "s": 0.0, "q": 0})
                a["s"] += s
                a["q"] += q
        top = sorted(agg.values(), key=lambda a: -a["s"])[:5]
        periods[str(p)] = {"top": [{**a, "s": round(a["s"], 2)} for a in top], "countries": []}
    return per, periods, truncated


def _fold(per, tz):
    today = datetime.now(tz).date()
    out = []
    for i in range(DAYS - 1, -1, -1):
        k = (today - timedelta(days=i)).isoformat()
        x = per.get(k, {})
        out.append({"d": k, "s": x.get("s", 0), "o": x.get("o", 0), "v": x.get("v")})
    return out


def _fetch(ctx, store):
    c = Client(ctx, store, time.time() + DEADLINE_S)
    shop = c.gql(SHOP_Q)["shop"]
    try:
        tz = ZoneInfo(shop.get("ianaTimezone") or "UTC")
    except Exception:
        tz = timezone.utc
    truncated = False
    try:
        per, periods, visitors = _from_shopifyql(c)
        source = "shopifyql"
    except ShopError as e:
        if e.code in ("auth", "network", "timeout"):
            raise
        per, periods, truncated = _from_orders(c, tz)
        visitors, source = False, "orders"
    try:
        last = [{"n": o["name"], "at": o["createdAt"], "total": round(_num(o["totalPriceSet"]["shopMoney"]["amount"]), 2),
                 "pay": (o.get("displayFinancialStatus") or "").lower(),
                 "ship": (o.get("displayFulfillmentStatus") or "").lower(), "canceled": bool(o.get("cancelledAt"))}
                for o in c.gql(LAST_Q)["orders"]["nodes"]]
    except ShopError:
        last = []
    handle = (shop.get("myshopifyDomain") or store).split(".")[0]
    return {"shop": {"name": shop.get("name") or handle, "currency": shop.get("currencyCode") or "USD",
                     "url": (shop.get("primaryDomain") or {}).get("url") or f"https://{store}",
                     "admin": f"https://admin.shopify.com/store/{handle}"},
            "tz": str(tz), "source": source, "truncated": truncated, "visitors": bool(visitors),
            "days": _fold(per, tz), "periods": periods, "last": last, "at": int(time.time())}


# ----------------------------------------------------------------------------- demo data

DEMO_PRODUCTS = ["Affiche Montagne Brume", "Poster Vague Indigo", "Cadre chêne 50x70", "Affiche Tokyo Nuit",
                 "Poster Botanique", "Affiche Désert Rose", "Poster Lune Minimal"]
DEMO_COUNTRIES = ["France", "Germany", "Belgium", "Switzerland", "Canada", "Spain"]


def _demo(ctx):
    """Plausible, made up figures (fixed seed per day) for previews and screenshots."""
    try:
        tz = ZoneInfo(ctx["config"].get("timezone") or "UTC")
    except Exception:
        tz = timezone.utc
    today = datetime.now(tz).date()
    rnd = random.Random(2026)                    # same figures on every call
    per = {}
    for i in range(DAYS - 1, -1, -1):
        d = today - timedelta(days=i)
        trend = 1 + (DAYS - i) / DAYS * 0.35
        week = 1.18 if d.weekday() in (5, 6) else 1.0
        visitors = int(rnd.gauss(1450, 160) * trend * week)
        orders = max(1, int(visitors * rnd.uniform(0.011, 0.019)))
        per[d.isoformat()] = {"s": round(orders * rnd.uniform(38, 52), 2), "o": orders, "v": visitors}
    k = today.isoformat()                        # today is not over: about 60 % of a full day
    per[k] = {"s": round(per[k]["s"] * 0.6, 2), "o": max(1, int(per[k]["o"] * 0.6)), "v": int(per[k]["v"] * 0.6)}
    periods = {}
    for p in PERIODS:
        total = sum(per[(today - timedelta(days=i)).isoformat()]["s"] for i in range(p))
        weights = sorted((rnd.uniform(0.4, 1.0) for _ in DEMO_PRODUCTS), reverse=True)
        names = rnd.sample(DEMO_PRODUCTS, len(DEMO_PRODUCTS))
        top = [{"t": n, "s": round(total * w / sum(weights) * 0.8, 2), "q": max(1, int(total * w / sum(weights) / 40))}
               for n, w in zip(names, weights, strict=True)][:5]
        shares = [0.46, 0.18, 0.12, 0.09, 0.06]
        periods[str(p)] = {"top": top, "countries": [{"c": c, "s": round(total * s, 2)}
                                                     for c, s in zip(DEMO_COUNTRIES, shares, strict=False)]}
    now = datetime.now(timezone.utc)
    pays = ["paid", "paid", "paid", "pending", "paid", "refunded"]
    ships = ["unfulfilled", "unfulfilled", "fulfilled", "unfulfilled", "fulfilled", "fulfilled"]
    last, t = [], now
    for i in range(6):
        t -= timedelta(minutes=rnd.randint(12, 140))
        last.append({"n": f"#{4821 - i}", "at": t.isoformat().replace("+00:00", "Z"),
                     "total": round(rnd.uniform(29, 129), 2), "pay": pays[i], "ship": ships[i], "canceled": False})
    return {"shop": {"name": "Atelier Nord", "currency": "EUR", "url": "https://example.com",
                     "admin": "https://admin.shopify.com"},
            "tz": str(tz), "source": "demo", "truncated": False, "visitors": True,
            "days": _fold(per, tz), "periods": periods, "last": last, "at": int(time.time())}


# ----------------------------------------------------------------------------- entry point

def _lang(ctx):
    return "fr" if str(ctx["config"].get("language") or "").startswith("fr") else "en"


def handle(ctx, method, path, query, body):
    if method != "GET" or path != "stats":
        return 404, {"error": "not_found", "message": f"{method} {path}"}
    lang = _lang(ctx)
    if str((query or {}).get("demo", "")) == "1" or _secret(ctx, "SHOPIFY_STATS_DEMO") == "1":
        return {"ok": True, "lang": lang, "demo": True, "data": _demo(ctx)}
    store = _store(ctx)
    if not store:
        return {"ok": False, "lang": lang, "error": "missing_keys"}
    cache = Path(ctx["state_dir"]) / "stats.json"
    force = str((query or {}).get("force", "")) == "1"
    try:
        c = json.loads(cache.read_text())
        age = time.time() - c["at"]
        if c["store"] == store and (age < FORCE_MIN_S or (age < CACHE_S and not force)):
            return {"ok": True, "lang": lang, "demo": False, "data": c["data"]}
    except (OSError, ValueError, KeyError):
        c = None
    try:
        data = _fetch(ctx, store)
    except (ShopError, KeyError, TypeError) as e:   # Key/TypeError: an answer of an unexpected shape
        stale = c["data"] if c and c.get("store") == store else None
        code = e.code if isinstance(e, ShopError) else "failed"
        return {"ok": bool(stale), "lang": lang, "demo": False, "data": stale, "error": code, "message": str(e)[:200]}
    _write_private(cache, {"at": time.time(), "store": store, "data": data})
    return {"ok": True, "lang": lang, "demo": False, "data": data}

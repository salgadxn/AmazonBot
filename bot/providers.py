"""Pricing data providers, each with its own free allowance, usage tracking,
per-run budget and clear error reporting.

  Provider   Free allowance            Used for
  --------   -----------------------   ------------------------------------------
  Best Buy   50,000 calls/day          exact UPC price + online stock (100 UPCs/call)
  Serper     2,500 credits, one-time   Google Shopping discovery (main workhorse)
  SerpApi    250 searches/month        verification: all-store comparison (stock +
                                       direct links), Walmart UPC check
  Tavily     1,000 credits/month       last-resort page discovery (price = page text)

Usage is stored in data/amazonbot.db so counts survive between runs.
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import re
import time

import requests

log = logging.getLogger(__name__)


def _days_left_in_month() -> int:
    t = dt.date.today()
    return calendar.monthrange(t.year, t.month)[1] - t.day + 1


def parse_money(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    m = re.search(r"\$?\s*(\d{1,5}(?:,\d{3})*(?:\.\d{1,2})?)", v)
    return float(m.group(1).replace(",", "")) if m else None


class Provider:
    name = "base"
    period_kind = "month"          # month / day / lifetime
    unit = "searches"

    def __init__(self, key: str, cfg: dict, store, http=None):
        self.key = key
        self.cfg = cfg
        self.pcfg = (cfg.get("providers") or {}).get(self.name, {}) or {}
        self.store = store
        self.http = http or requests.Session()
        self.calls = 0              # paid calls this run
        self.cache_hits = 0
        self.errors: list[str] = []
        self.budget = 0
        self.plan_left: int | None = None
        self.limit_note = ""
        self.disabled_reason = ""
        if not key:
            self.disabled_reason = f"no {self.secret_name} secret"
        elif self.pcfg.get("enabled") is False:
            self.disabled_reason = "turned off in config.yaml"
        else:
            self._init_budget()

    secret_name = ""

    # ---- usage persistence
    def period(self) -> str:
        t = dt.date.today()
        return {"month": t.strftime("%Y-%m"), "day": t.isoformat(), "lifetime": "lifetime"}[self.period_kind]

    def used_in_period(self) -> int:
        return self.store.usage_get(self.name, self.period())

    def _count(self, n: int = 1):
        self.calls += n
        self.store.usage_add(self.name, self.period(), n)

    # ---- budget
    def _init_budget(self):
        raise NotImplementedError

    @property
    def active(self) -> bool:
        return not self.disabled_reason and self.calls < self.budget

    def remaining_run(self) -> int:
        return max(self.budget - self.calls, 0) if not self.disabled_reason else 0

    def fail(self, msg: str, disable: bool = False):
        self.errors.append(msg[:200])
        log.warning("%s: %s", self.name, msg[:200])
        if disable:
            self.disabled_reason = msg[:150]

    def report(self) -> dict:
        left = self.plan_left
        return {
            "Provider": self.name,
            "Status": "OFF — " + self.disabled_reason if self.disabled_reason and not self.calls
            else ("STOPPED — " + self.disabled_reason if self.disabled_reason else "OK"),
            "Used this run": self.calls, "Cache hits (free)": self.cache_hits,
            "Run budget": self.budget,
            "Left on free plan": "" if left is None else max(left - self.calls, 0),
            "Free limit": self.limit_note,
            "Errors": " | ".join(self.errors[:3]),
        }

    # ---- cache helpers (shared table, keys are provider-independent where possible)
    def cached(self, key: str, days: int):
        hit = self.store.cached_serp(key, days)
        if hit is not None:
            self.cache_hits += 1
        return hit

    def save(self, key: str, data, fetched: str):
        self.store.save_serp(key, [data, fetched])


# ====================================================================== SerpApi

class SerpApiProvider(Provider):
    name = "SerpApi"
    secret_name = "SERPAPI_KEY"
    URL = "https://serpapi.com/search.json"

    def _init_budget(self):
        cap = int(self.pcfg.get("max_per_run", 40))
        try:
            r = self.http.get("https://serpapi.com/account.json", params={"api_key": self.key}, timeout=30)
            data = r.json()
            if r.status_code != 200 or data.get("error"):
                self.disabled_reason = f"key rejected: {data.get('error', r.status_code)}"
                return
            left = data.get("total_searches_left", data.get("plan_searches_left"))
            self.plan_left = int(left) if left is not None else None
            per_month = data.get("searches_per_month")
            self.limit_note = f"{per_month or 250}/month (resets monthly)"
        except Exception as exc:
            self.fail(f"account check failed: {exc}")
        if self.plan_left is None:
            self.budget = cap
            return
        if self.plan_left <= 0:
            self.disabled_reason = "0 searches left this month"
            return
        reserve = int(self.pcfg.get("reserve", 5))
        self.budget = min(cap, max(1, (self.plan_left - reserve) // _days_left_in_month()))

    def _get(self, params: dict, cache_key: str, days: int):
        hit = self.cached(cache_key, days)
        if hit is not None:
            return hit
        if not self.active:
            return None, ""
        self._count()
        try:
            r = self.http.get(self.URL, params={**params, "api_key": self.key}, timeout=90)
            data = r.json()
        except Exception as exc:
            self.fail(f"{params.get('engine')}: {exc}")
            return None, ""
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            if "hasn't returned any results" in err or "no results" in err.lower():
                data = {}
            else:
                self.fail(f"{params.get('engine')}: {err}",
                          disable="run out" in err.lower() or "invalid api key" in err.lower())
                return None, ""
        fetched = dt.date.today().isoformat()
        self.save(cache_key, data, fetched)
        return data, fetched

    def shopping(self, query: str, days: int):
        """Google Shopping -> (normalized results, fetched). Results keep immersive tokens."""
        data, fetched = self._get({"engine": "google_shopping", "q": query, "gl": "us", "hl": "en",
                                   "location": "United States"}, f"serpapi-gs:{query}", days)
        if data is None:
            return None, ""
        out = []
        for r in data.get("shopping_results", []) or []:
            out.append({"title": r.get("title", ""), "source": r.get("source", ""),
                        "price": r.get("extracted_price"),
                        "link": r.get("product_link") or r.get("link", ""),
                        "delivery": r.get("delivery", ""), "extensions": r.get("extensions") or [],
                        "used": bool(r.get("second_hand_condition")),
                        "token": r.get("immersive_product_page_token", "")})
        return out, fetched

    def compare_stores(self, token: str, days: int):
        return self._get({"engine": "google_immersive_product", "page_token": token, "more_stores": "true"},
                         f"im:{token[:150]}", days)

    def walmart_upc(self, upc: str, days: int):
        return self._get({"engine": "walmart_product", "upc": upc}, f"wm:{upc}", days)


# ====================================================================== Serper

class SerperProvider(Provider):
    name = "Serper"
    secret_name = "SERPER_API_KEY"
    period_kind = "lifetime"
    URL = "https://google.serper.dev/shopping"

    def _init_budget(self):
        start = int(self.pcfg.get("starting_credits", 2500))
        used = self.used_in_period()
        self.plan_left = max(start - used, 0)
        self.limit_note = f"{start} one-time free credits (do not refill; valid ~6 months)"
        if self.plan_left <= 0:
            self.disabled_reason = "free credits used up (update providers.serper.starting_credits if you top up)"
            return
        self.budget = min(int(self.pcfg.get("max_per_run", 80)), self.plan_left)

    def shopping(self, query: str, days: int):
        key = f"serper-gs:{query}"
        hit = self.cached(key, days)
        if hit is not None:
            return hit
        if not self.active:
            return None, ""
        try:
            r = self.http.post(self.URL, json={"q": query, "gl": "us", "hl": "en"},
                               headers={"X-API-KEY": self.key, "Content-Type": "application/json"},
                               timeout=60)
            data = r.json() if r.content else {}
        except Exception as exc:
            self.fail(f"shopping: {exc}")
            return None, ""
        self._count(int(data.get("credits", 1) or 1) if isinstance(data, dict) else 1)
        if r.status_code >= 400:
            msg = (data or {}).get("message", f"HTTP {r.status_code}") if isinstance(data, dict) else str(r.status_code)
            self.fail(f"shopping: {msg}", disable=r.status_code in (401, 403) or "credit" in msg.lower())
            return None, ""
        out = []
        for s in data.get("shopping", []) or []:
            out.append({"title": s.get("title", ""), "source": s.get("source", ""),
                        "price": parse_money(s.get("price")), "link": s.get("link", ""),
                        "delivery": s.get("delivery", ""), "extensions": [], "used": False, "token": ""})
        fetched = dt.date.today().isoformat()
        self.save(key, out, fetched)
        return out, fetched


# ====================================================================== Tavily

class TavilyProvider(Provider):
    name = "Tavily"
    secret_name = "TAVILY_API_KEY"
    URL = "https://api.tavily.com/search"

    def _init_budget(self):
        monthly = int(self.pcfg.get("monthly_credits", 1000))
        used = self.used_in_period()
        self.plan_left = max(monthly - used, 0)
        self.limit_note = f"{monthly} credits/month (resets monthly)"
        if self.plan_left <= 0:
            self.disabled_reason = "monthly credits used up"
            return
        daily = max(1, self.plan_left // _days_left_in_month())
        self.budget = min(int(self.pcfg.get("max_per_run", 30)), daily)

    def search(self, query: str, domains: list[str], days: int):
        key = f"tavily:{query}"
        hit = self.cached(key, days)
        if hit is not None:
            return hit
        if not self.active:
            return None, ""
        self._count()
        try:
            r = self.http.post(self.URL, json={"query": query, "search_depth": "basic", "max_results": 8,
                                               "include_domains": domains},
                               headers={"Authorization": f"Bearer {self.key}"}, timeout=60)
            data = r.json() if r.content else {}
        except Exception as exc:
            self.fail(f"search: {exc}")
            return None, ""
        if r.status_code >= 400:
            msg = (data.get("detail") or data.get("error") or f"HTTP {r.status_code}") if isinstance(data, dict) else str(r.status_code)
            self.fail(f"search: {msg}", disable=r.status_code in (401, 403, 429, 432, 433))
            return None, ""
        out = [{"title": x.get("title", ""), "url": x.get("url", ""), "content": x.get("content", "")}
               for x in data.get("results", []) or []]
        fetched = dt.date.today().isoformat()
        self.save(key, out, fetched)
        return out, fetched


# ====================================================================== Best Buy

class BestBuyProvider(Provider):
    name = "Best Buy"
    secret_name = "BESTBUY_API_KEY"
    period_kind = "day"
    URL = "https://api.bestbuy.com/v1/products({query})"
    FIELDS = "sku,name,upc,salePrice,regularPrice,onlineAvailability,url,orderable"

    def _init_budget(self):
        daily = int(self.pcfg.get("daily_calls", 50000))
        self.plan_left = max(daily - self.used_in_period(), 0)
        self.limit_note = f"{daily:,} calls/day, 5/second"
        self.budget = min(int(self.pcfg.get("max_per_run", 2000)), self.plan_left)

    def lookup_upcs(self, upcs: list[str], days: int) -> dict[str, tuple[dict, str]]:
        """Returns {upc12: (product, fetched)} for UPCs Best Buy carries. 100 UPCs per call."""
        out: dict[str, tuple[dict, str]] = {}
        todo = []
        for u in dict.fromkeys(upcs):
            hit = self.cached(f"bby:{u}", days)
            if hit is not None:
                if hit[0]:
                    out[u] = (hit[0], hit[1])
            else:
                todo.append(u)
        for i in range(0, len(todo), 100):
            if not self.active:
                break
            chunk = todo[i:i + 100]
            self._count()
            try:
                r = self.http.get(self.URL.format(query=f"upc in({','.join(chunk)})"),
                                  params={"apiKey": self.key, "format": "json", "show": self.FIELDS,
                                          "pageSize": 100}, timeout=60)
                data = r.json() if r.content else {}
            except Exception as exc:
                self.fail(f"products: {exc}")
                break
            if r.status_code == 403:
                self.fail("403 — invalid key or daily/second limit reached", disable=True)
                break
            if r.status_code >= 400:
                self.fail(f"products: HTTP {r.status_code}")
                continue
            fetched = dt.date.today().isoformat()
            found = {}
            for p in data.get("products", []) or []:
                u = str(p.get("upc", "")).zfill(12)[-12:]
                found[u] = p
            for u in chunk:
                p = found.get(u.zfill(12)[-12:])
                self.save(f"bby:{u}", p or {}, fetched)   # also cache "not carried"
                if p:
                    out[u] = (p, fetched)
            time.sleep(0.25)   # stay under 5 calls/second
        return out

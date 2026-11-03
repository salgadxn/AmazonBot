"""SQLite storage: daily snapshots (the bot's own price history), eligibility
cache, brand list, alert de-duplication and supplier-lookup cache.

The file lives at data/amazonbot.db and is committed back to the repo by the
GitHub workflow so history builds up day after day."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  asin TEXT, day TEXT, buybox REAL, fba_offers INT, fbm_offers INT,
  total_offers INT, amazon_on INT, rank INT, PRIMARY KEY (asin, day));
CREATE TABLE IF NOT EXISTS eligibility (
  asin TEXT PRIMARY KEY, status TEXT, message TEXT, link TEXT, checked TEXT);
CREATE TABLE IF NOT EXISTS brands (
  brand TEXT PRIMARY KEY, source TEXT, status TEXT, eligible_count INT DEFAULT 0,
  checked_count INT DEFAULT 0, last_checked TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  asin TEXT PRIMARY KEY, sent TEXT, profit REAL, eligibility TEXT);
CREATE TABLE IF NOT EXISTS provider_usage (
  provider TEXT, period TEXT, used INT DEFAULT 0, PRIMARY KEY (provider, period));
CREATE TABLE IF NOT EXISTS sourcing_log (
  asin TEXT PRIMARY KEY, day TEXT, outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS serp_cache (
  upc TEXT PRIMARY KEY, fetched TEXT, json TEXT);  -- upc column holds any cache key
"""


def today() -> str:
    return dt.date.today().isoformat()


@dataclass
class HistoryReport:
    days: int = 0
    stability: str = "Not enough history"
    flags: list[tuple[str, str]] = field(default_factory=list)  # (key, text)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.commit()
        self.db.close()

    # ---------------- snapshots / history
    def save_snapshot(self, asin, buybox, fba, fbm, total, amazon_on, rank, day=None):
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
            (asin, day or today(), buybox, fba, fbm, total, int(bool(amazon_on)), rank))

    def history(self, asin: str, days: int = 90) -> list[tuple]:
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        return self.db.execute(
            "SELECT day, buybox, total_offers, amazon_on FROM snapshots "
            "WHERE asin=? AND day>=? ORDER BY day", (asin, since)).fetchall()

    def analyze(self, asin: str, current_price: float | None,
                current_offers: int) -> HistoryReport:
        rows = self.history(asin)
        rep = HistoryReport(days=len(rows))
        if not rows:
            return rep
        if any(r[3] for r in rows):
            rep.flags.append(("amazon_recent", "Amazon sold this in the last 90 days"))

        recent = rows[-4:]
        peak = max((r[2] or 0) for r in recent)
        if peak >= 5 and current_offers <= peak * 0.6:
            rep.flags.append(("ip_drop", f"Sellers dropped {peak}->{current_offers} in a few days "
                                         "(possible IP complaint)"))

        prices = [r[1] for r in rows[-30:] if r[1]]
        if len(prices) >= 7:
            med = statistics.median(prices)
            if current_price and current_price > med * 1.25:
                rep.flags.append(("price_spike", f"Price spike: now ${current_price:.2f} vs usual ${med:.2f}"))
            cv = statistics.pstdev(prices) / statistics.mean(prices)
            rep.stability = "Stable" if cv < 0.08 else "Moderate" if cv < 0.2 else "Volatile"
        else:
            rep.stability = f"Building history ({len(rows)}d)"
        return rep

    # ---------------- eligibility cache
    def cached_eligibility(self, asin: str, max_age_days: int):
        row = self.db.execute(
            "SELECT status, message, link, checked FROM eligibility WHERE asin=?",
            (asin,)).fetchone()
        if not row:
            return None
        if dt.date.fromisoformat(row[3]) < dt.date.today() - dt.timedelta(days=max_age_days):
            return None
        return row[:3]

    def save_eligibility(self, asin, status, message, link):
        self.db.execute("INSERT OR REPLACE INTO eligibility VALUES (?,?,?,?,?)",
                        (asin, status, message, link, today()))

    # ---------------- brands
    def add_brand(self, brand: str, source: str):
        self.db.execute("INSERT OR IGNORE INTO brands (brand, source, status) VALUES (?,?,?)",
                        (brand, source, "new"))

    def brands_to_scan(self, limit: int) -> list[str]:
        rows = self.db.execute(
            "SELECT brand FROM brands WHERE status != 'skip' "
            "ORDER BY (last_checked IS NOT NULL), last_checked, eligible_count DESC "
            "LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    def update_brand(self, brand: str, eligible: int, checked: int):
        status = "eligible" if eligible else ("gated" if checked else "no results")
        self.db.execute(
            "UPDATE brands SET status=?, eligible_count=?, checked_count=?, last_checked=? "
            "WHERE brand=?", (status, eligible, checked, today(), brand))

    def all_brands(self) -> list[tuple]:
        return self.db.execute(
            "SELECT brand, source, status, eligible_count, checked_count, last_checked "
            "FROM brands ORDER BY eligible_count DESC, brand").fetchall()

    def known_brand(self, brand: str) -> bool:
        return self.db.execute("SELECT 1 FROM brands WHERE lower(brand)=lower(?)",
                               (brand,)).fetchone() is not None

    # ---------------- alerts
    def should_alert(self, asin, profit, eligibility, cfg_alerts) -> bool:
        row = self.db.execute("SELECT sent, profit, eligibility FROM alerts WHERE asin=?",
                              (asin,)).fetchone()
        if not row:
            return True
        sent, old_profit, old_elig = row
        if old_elig != eligibility:
            return True
        if old_profit and abs(profit - old_profit) / abs(old_profit) >= cfg_alerts["realert_profit_change"]:
            return True
        age = (dt.date.today() - dt.date.fromisoformat(sent)).days
        return age >= cfg_alerts["realert_after_days"]

    def mark_alerted(self, asin, profit, eligibility):
        self.db.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?)",
                        (asin, today(), profit, eligibility))

    # ---------------- supplier cache
    def cached_serp(self, upc: str, max_age_days: int):
        row = self.db.execute("SELECT fetched, json FROM serp_cache WHERE upc=?",
                              (upc,)).fetchone()
        if not row or dt.date.fromisoformat(row[0]) < dt.date.today() - dt.timedelta(days=max_age_days):
            return None
        return json.loads(row[1])

    def save_serp(self, upc: str, data):
        self.db.execute("INSERT OR REPLACE INTO serp_cache VALUES (?,?,?)",
                        (upc, today(), json.dumps(data)))

    # ---------------- provider usage
    def usage_get(self, provider: str, period: str) -> int:
        row = self.db.execute("SELECT used FROM provider_usage WHERE provider=? AND period=?",
                              (provider, period)).fetchone()
        return row[0] if row else 0

    def usage_add(self, provider: str, period: str, n: int = 1):
        self.db.execute("INSERT INTO provider_usage VALUES (?,?,?) ON CONFLICT(provider, period) "
                        "DO UPDATE SET used = used + ?", (provider, period, n, n))
        self.db.commit()

    # ---------------- sourcing log (skip re-searching recent dead ends)
    def last_sourcing(self, asin: str):
        return self.db.execute("SELECT day, outcome, detail FROM sourcing_log WHERE asin=?",
                               (asin,)).fetchone()

    def log_sourcing(self, asin: str, outcome: str, detail: str):
        self.db.execute("INSERT OR REPLACE INTO sourcing_log VALUES (?,?,?,?)",
                        (asin, today(), outcome, detail[:300]))

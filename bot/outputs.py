"""Write results to CSV files, Google Sheets and Discord."""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)

_PROFIT_COLS = [
    "Amazon price", "Est. sales/mo (estimate)", "Sales rank", "FBA sellers", "FBM sellers",
    "Eligibility", "Referral fee", "FBA fee", "Closing fee", "Storage (est)",
    "Returns reserve (est)", "Inbound ship", "Prep+misc", "Tax", "Retailer ship/unit",
    "Total cost (landed)", "Net profit", "ROI", "Margin", "Profit if price -10%",
    "Profit w/ savings", "ROI w/ savings", "Certain savings", "Rule", "Suggested qty",
    "Price stability", "Flags", "Notes", "Last checked",
]
_OFFER_COLS = [
    "Retailer", "Buy link", "Advertised price", "Units per Amazon unit", "Cost per Amazon unit",
    "Retailer shipping", "Availability", "Price checked", "Match", "Match details",
    "Other suppliers",
]
_ID_COLS = ["Score", "Product", "ASIN", "Amazon link", "Brand", "Category"]

LEAD_COLUMNS = _ID_COLS + _OFFER_COLS + _PROFIT_COLS
REVIEW_COLUMNS = _ID_COLS[:3] + ["Why not a Lead"] + _ID_COLS[3:] + _OFFER_COLS + _PROFIT_COLS
OPTION_COLUMNS = [
    "ASIN", "Product", "Retailer", "Trusted store", "Advertised price", "Units per Amazon unit",
    "Cost per Amazon unit", "Retailer shipping", "Availability", "Match", "Match details",
    "Net profit", "ROI", "Can be a Lead", "Why not", "Buy link", "Source", "Price checked",
]
NO_SUPPLIER_COLUMNS = [
    "Score", "Product", "ASIN", "Amazon link", "Brand", "Category", "UPC", "Amazon price",
    "Max buy price", "Est. sales/mo (estimate)", "Sales rank", "FBA sellers", "Eligibility",
    "Why no lead", "Flags", "Google search", "Last checked",
]
USAGE_COLUMNS = ["Provider", "Status", "Used this run", "Cache hits (free)", "Run budget",
                 "Left on free plan", "Free limit", "Errors"]
BRAND_COLUMNS = ["Brand", "Source", "Status", "Eligible products", "Products checked",
                 "Last checked"]


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: _cell(v) for k, v in r.items()} for r in rows)


def write_sheets(sheet_id: str, sa_json: str, tabs: dict[str, tuple[list[str], list[dict]]]) -> None:
    import gspread  # imported lazily so local runs work without it

    try:
        info = json.loads(sa_json)
    except json.JSONDecodeError:
        raise SystemExit("GOOGLE_SERVICE_ACCOUNT_JSON must be the full contents of the key file.")
    gc = gspread.service_account_from_dict(info)
    sh = gc.open_by_key(sheet_id)
    existing = {ws.title: ws for ws in sh.worksheets()}
    for title, (columns, rows) in tabs.items():
        ws = existing.get(title) or sh.add_worksheet(title, rows=max(len(rows) + 10, 100),
                                                     cols=len(columns) + 2)
        values = [columns] + [[_cell(r.get(c, "")) for c in columns] for r in rows]
        ws.clear()
        ws.update(values, "A1", value_input_option="USER_ENTERED")
        try:
            ws.freeze(rows=1)
        except Exception:  # cosmetic only
            pass
    log.info("Google Sheet updated (%s)", ", ".join(tabs))


def _cell(v):
    if isinstance(v, float):
        return round(v, 2)
    return v


def discord_alert(webhook: str, lead: dict) -> None:
    buy = f"${lead['Cost per Amazon unit']:.2f} at {lead['Retailer']}"
    if lead.get("Units per Amazon unit", 1) > 1:
        buy += f" (buy {lead['Units per Amazon unit']})"
    fields = [
        {"name": "Buy", "value": buy, "inline": True},
        {"name": "Sell", "value": f"${lead['Amazon price']:.2f}", "inline": True},
        {"name": "Profit / ROI", "value": f"${lead['Net profit']:.2f} / {lead['ROI']}", "inline": True},
        {"name": "Est. sales/mo", "value": str(lead["Est. sales/mo (estimate)"]), "inline": True},
        {"name": "FBA sellers", "value": str(lead["FBA sellers"]), "inline": True},
        {"name": "Match", "value": f"{lead['Match']} · {lead['Availability']}", "inline": True},
    ]
    if lead.get("Flags"):
        fields.append({"name": "⚠️ Flags", "value": lead["Flags"][:1000]})
    payload = {"embeds": [{
        "title": lead["Product"][:250],
        "url": lead["Amazon link"],
        "description": f"[Buy at {lead['Retailer']}]({lead['Buy link']})" if lead.get("Buy link") else "",
        "color": 0x2ECC71,
        "fields": fields,
        "footer": {"text": f"Score {lead['Score']} • price checked {lead['Price checked']} • "
                           "check the chart in SellerAmp before buying"},
    }]}
    resp = requests.post(webhook, json=payload, timeout=30)
    if resp.status_code >= 300:
        log.warning("Discord alert failed: %s %s", resp.status_code, resp.text[:200])


def discord_summary(webhook: str, text: str) -> None:
    requests.post(webhook, json={"content": text[:1900]}, timeout=30)

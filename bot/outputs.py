"""Write results to CSV files, Google Sheets and Discord."""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)

LEAD_COLUMNS = [
    "Score", "Product", "ASIN", "Amazon link", "Brand", "Category",
    "Supplier", "Supplier link", "Match", "Buy price", "Amazon price",
    "Est. sales/mo (estimate)", "Sales rank", "FBA sellers", "FBM sellers",
    "Amazon on listing", "Eligibility", "Apply link",
    "Referral fee", "FBA fee", "Closing fee", "Storage (est)", "Returns reserve (est)",
    "Inbound ship", "Prep+misc", "Tax", "Total cost (landed)",
    "Net profit", "ROI", "Margin", "Profit if price -10%",
    "Profit w/ savings", "ROI w/ savings", "Certain savings",
    "Rule", "Suggested qty", "Price stability", "Red flags", "Notes", "Last checked",
]

FIND_SUPPLIER_COLUMNS = [
    "Score", "Product", "ASIN", "Amazon link", "Brand", "Category", "UPC",
    "Amazon price", "Max buy price (Standard)", "Max buy price (Starter)",
    "Est. sales/mo (estimate)", "Sales rank", "FBA sellers", "FBM sellers",
    "Eligibility", "Referral fee", "FBA fee", "Price stability", "Red flags",
    "Google search", "Last checked",
]

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
    fields = [
        {"name": "Buy", "value": f"${lead['Buy price']:.2f} @ {lead['Supplier']}", "inline": True},
        {"name": "Sell", "value": f"${lead['Amazon price']:.2f}", "inline": True},
        {"name": "Profit / ROI", "value": f"${lead['Net profit']:.2f} / {lead['ROI']}", "inline": True},
        {"name": "Est. sales/mo", "value": str(lead["Est. sales/mo (estimate)"]), "inline": True},
        {"name": "FBA sellers", "value": str(lead["FBA sellers"]), "inline": True},
        {"name": "Match", "value": lead["Match"], "inline": True},
    ]
    if lead.get("Red flags"):
        fields.append({"name": "⚠️ Flags", "value": lead["Red flags"][:1000]})
    payload = {"embeds": [{
        "title": lead["Product"][:250],
        "url": lead["Amazon link"],
        "description": f"[Supplier link]({lead['Supplier link']})" if lead.get("Supplier link") else "",
        "color": 0x2ECC71,
        "fields": fields,
        "footer": {"text": f"Score {lead['Score']} • {lead['Eligibility']} • check the chart in SellerAmp before buying"},
    }]}
    resp = requests.post(webhook, json=payload, timeout=30)
    if resp.status_code >= 300:
        log.warning("Discord alert failed: %s %s", resp.status_code, resp.text[:200])


def discord_summary(webhook: str, text: str) -> None:
    requests.post(webhook, json={"content": text[:1900]}, timeout=30)

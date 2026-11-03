# AmazonBot — FBA sourcing lead finder

Runs every morning on GitHub (your computer can be off) and:

1. **Finds products**: scans store brands and discovers new brands you can sell
2. **Checks your eligibility**: uses your own Seller Central account (Eligible / Needs approval / Restricted)
3. **Reads live Amazon data**: buy box price, FBA/FBM sellers, whether Amazon is selling, sales rank
4. **Pulls Amazon's real fees** (referral + FBA) and adds your costs: Chicago tax, inbound shipping, prep, storage, returns
5. **Finds where to buy**: from your wholesale price lists and Google Shopping (by UPC)
6. **Writes a Google Sheet** and pings **Discord** for strong leads

It never buys anything. You review every lead, check the chart in SellerAmp, then decide.

---

## The Google Sheet tabs

| Tab | What it is |
|---|---|
| **Leads** | Eligible products **with a supplier price** that meet your profit/ROI rules. Sorted by Score. |
| **Find Supplier** | Eligible, good-demand products with no supplier found yet. Shows the **max buy price** that still works, plus a Google Shopping link. |
| **Brands** | Every brand scanned, and how many of its products you're eligible for. |
| **Run Log** | What happened on the last run. |

**Verified vs estimate:** fees, eligibility, live price and seller counts come straight from Amazon (verified).
Columns marked *(est)* / *(estimate)* are estimates. Monthly sales are a **rough** guess from sales rank until Keepa is added.
`Match` says how sure the supplier match is: `UPC (price list)` is exact, `UPC search` is a strong title match, and **`Weak match - verify`** means you should check the size and pack count yourself.

**Red flags** the bot looks for: Amazon selling now or in the last 90 days, sellers dropping fast (possible IP complaint),
price spikes, a merchant-fulfilled seller holding the buy box, and no buy box. History-based flags start working after
~1–2 weeks of daily runs, because the bot builds its own price history. Until then, check the Keepa/SellerAmp chart yourself.
It **cannot** tell if the brand owner is a seller. Check the seller names on the listing.

---

## One-time setup (about 20 minutes)

### 1. Add your Amazon keys to GitHub (required)
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add each one:

| Name | Value |
|---|---|
| `SPAPI_LWA_CLIENT_ID` | Seller Central → Develop Apps → your app → LWA credentials → Client ID |
| `SPAPI_LWA_CLIENT_SECRET` | same place → Client Secret |
| `SPAPI_REFRESH_TOKEN` | the token from "Authorize app" (starts `Atzr|`) |
| `SPAPI_SELLER_ID` | Settings → Account Info → Merchant Token |

### 2. Test the keys
Repo → **Actions → Daily sourcing run → Run workflow → mode: `check`** → Run.
Open the run. You should see "Everything works." If not, the error tells you which key to fix.

### 3. Google Sheet (recommended)
1. Go to <https://console.cloud.google.com> → create a project (any name).
2. **APIs & Services → Library**: enable **Google Sheets API** and **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create service account** (any name) → Done.
4. Click the account → **Keys → Add key → Create new key → JSON**. A file downloads.
5. Create a new Google Sheet. Click **Share** and add the service account's email (ends in `iam.gserviceaccount.com`) as **Editor**.
6. Add secrets:
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: open the downloaded file in Notepad, copy **everything**, and paste it.
   - `GOOGLE_SHEET_ID`: the long code in the sheet URL between `/d/` and `/edit`.
7. Delete the downloaded key file from your Downloads folder afterwards.

Without this, results are still saved. Open a run → **Artifacts → results** (CSV files).

### 4. Discord alerts (recommended)
Your Discord server → channel ⚙️ **Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL**.
Add secret `DISCORD_WEBHOOK_URL`.

### 5. Supplier price search (optional, free)
Sign up at <https://serpapi.com> (free plan = 250 searches/month). Copy your API key and add secret `SERPAPI_KEY`.
The bot uses about 8 searches per day on the best candidates. Without it, use the **Find Supplier** tab.

---

## Changing settings
Edit **`config.yaml`** in GitHub (pencil icon → Commit). Every number is explained inside it:
profit/ROI rules, Starter mode, tax rate, inbound $/lb, prep, storage months, returns %, budget per product, alert thresholds.

Edit **`retailers.yaml`** about once a week with gift card % (CardBear) and cashback % (Rakuten).
- Set `giftcard_owned: true` / `store_card_owned: true` only when you actually have them. Only those count as "certain" savings.
- Set `tax_exempt: true` for a store once it has your resale certificate on file.

Edit **`seed_brands.txt`** to add brands you know you can sell.
Drop wholesale price lists in **`data/price_lists/`** (see the README there).

## Running on demand
**Actions → Daily sourcing run → Run workflow**:
- `full`: a normal run
- `check`: tests your keys
- `asins`: analyses specific products. Paste ASINs separated by commas.

## How Amazon limits affect speed
Amazon only allows a certain number of requests per second, so a run checks a few hundred to a couple thousand products
and takes roughly 20–90 minutes. Tune `search:` in `config.yaml` if needed.

## Adding Keepa later
Keepa gives full price/seller/buy-box history and real "bought in past month" numbers. When you subscribe to the Keepa API,
we'll add a `KEEPA_API_KEY` secret and a Keepa data source. Nothing else changes.

## Security
- Keys live only in GitHub Secrets (encrypted). Never put them in files or chats.
- If a key leaks, re-generate it in Seller Central and update the secret.
- Amazon requires rotating the LWA client secret periodically. If runs start failing with a login error, generate a new one and update `SPAPI_LWA_CLIENT_SECRET`.

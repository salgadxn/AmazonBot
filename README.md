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
| **Leads** | Only products with a **real store offer** that meets your profit and ROI rules. Each row has the store, a **direct buy link**, the advertised price, stock, the date the price was checked, the match level, other stores' prices, and the full profit breakdown. |
| **Needs Review** | Profitable offers the bot won't trust by itself. "Why not a Lead" says what to check: an uncertain size or pack match, an unknown store, a marketplace seller, stock not confirmed, or a listing flag. |
| **Supplier Options** | Every store offer found, one row per offer, so you can compare stores. |
| **No Supplier Yet** | Eligible products with no usable offer. "Why no lead" says exactly why: not searched yet (budget), no offers found, too expensive (with the cheapest price found), only different sizes, out of stock, or a listing problem. |
| **Brands** | Brands scanned and how many products you're eligible for. |
| **Run Log** | What happened, including whether supplier search is ON, how many SerpApi searches are left, and any errors. |

### How a supplier offer becomes a Lead
1. **Found** via your price lists, Google Shopping by UPC, Google's store comparison (every store's price, shipping and stock), and a Walmart UPC lookup.
2. **Matched**:
   - **Exact**: the UPC is confirmed.
   - **Strong**: brand, size or count, and pack all agree.
   - **Uncertain**: needs your eyes.
   - **Mismatch**: a different size or pack; never used.

   If Amazon sells a "Pack of 3" and the store sells singles, the bot works out the cost of buying 3.
3. **Costed** with tax, store shipping, Amazon fees, inbound shipping, prep, storage, returns, and your discounts.
4. **Lead only if all are verified**:
   - UPC confirmed (Walmart, Best Buy or a price list), or found by searching the exact UPC with brand, size and pack all agreeing
   - store shows **In stock**
   - a direct store link (not a Google redirect)
   - a price that wasn't just read from page text
   - a store in `retailers.yaml`
   - not a Walmart marketplace seller
   - price checked within 2 days
   - meets your profit and ROI rules
   - no serious listing flag: Amazon selling, possible IP complaint, crowded, seller-made bundle, or no buy box

   Hazmat, pesticide and CBD items are dropped completely. Supplements, medical devices, meltables, age-restricted items and expiration-dated items are flagged.

### Free pricing sources ($0/month) and how the bot uses them
| Source | Secret name | Free limit | What it gives | Role |
|---|---|---|---|---|
| Your price lists | (files in `data/price_lists/`) | unlimited | exact UPC, your cost, MOQ | checked first |
| Best Buy API | `BESTBUY_API_KEY` | 50,000 calls/day | exact UPC, price, online stock, direct link (electronics/toys/media mostly) | checked first, 100 UPCs per call |
| Serper.dev | `SERPER_API_KEY` | 2,500 credits **one-time** | Google Shopping prices by store (no stock) | main discovery |
| SerpApi | `SERPAPI_KEY` | 250/month | Google all-store comparison (stock + direct links), Walmart UPC check | verification (+ discovery backup) |
| Tavily | `TAVILY_API_KEY` | 1,000/month | store pages + page text (price not structured) | last resort; never a Lead by itself |

How searches are saved:
- Prices are cached for 2 days.
- Each UPC is searched once per run.
- Recent dead ends aren't searched again for a while: no offers (14 days), no match (10), too expensive (5).
- The best products are searched first.
- SerpApi is only used to **verify** offers that already look profitable.
- When one provider runs out, the bot switches to the next.
- The **API Usage** tab shows what's left on each provider.

Getting the keys (all free, no card needed except where a site asks):
- Serper: <https://serper.dev> → sign up → API key
- SerpApi: <https://serpapi.com> → sign up → API key
- Tavily: <https://app.tavily.com> → sign up → API key
- Best Buy: <https://developer.bestbuy.com> → Get API Key → activate by email

**Verified vs estimate:** fees, eligibility, live price and seller counts come straight from Amazon (verified).
Columns marked *(est)* / *(estimate)* are estimates. Monthly sales are a **rough** guess from sales rank until Keepa is added.

**Red flags** the bot looks for: Amazon selling now or in the last 90 days, sellers dropping fast (possible IP complaint),
price spikes, a merchant-fulfilled seller holding the buy box, and no buy box. History-based flags start working after
~1–2 weeks of daily runs. Until then, check the Keepa/SellerAmp chart yourself.
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

### 5. Supplier search (needed for Leads)
Sign up at <https://serpapi.com>. Copy your API key from the dashboard and add secret `SERPAPI_KEY`.
Then run the workflow in `check` mode. Line 5 should say "SerpApi: OK — N searches left".
Without the key, **Leads stays empty**, and No Supplier Yet says "Supplier search not run: SERPAPI_KEY secret is not set".

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

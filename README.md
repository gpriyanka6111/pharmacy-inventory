# Pharmacy Inventory

Scan a bottle, see the drug, add or remove stock. Multi-store, each store with its own login, plus history and Excel export.

## What's in here
| File | What it does |
|---|---|
| `app.py` | The web app (all pages) |
| `templates/`, `static/` | Page layouts, styles, scan screen script |
| `db.py` | Database tables |
| `barcode.py` | Turns a scanned barcode into a drug |
| `inventory.py` | Stock changes (with audit log); `transfer_stock()` is kept but not used by the app |
| `load_products.py` | Loads a Cardinal Health CSV export into the drug list |
| `render.yaml` | Hosting settings for Render |

## Run it on your computer (Windows PowerShell)
    cd C:\Users\User\Desktop\pharmacy-inventory
    venv\Scripts\activate
    pip install -r requirements.txt
    $env:DATABASE_URL="postgresql://...your Neon string..."
    python app.py
Then open http://127.0.0.1:5000 in your browser.

Without `$env:DATABASE_URL`, the app uses a local test file (inventory.db) instead of Neon.

## Update the drug list
Download a new Cardinal export (CSV, Unformatted, All results), then:

    python load_products.py Drug_Details.csv

Existing drugs get the latest price; new drugs are added. Stock counts are not touched.

## Accounts
Each store signs up for its own account (one account = one store). There are no admin-managed stores or users.

- **Owner**: created on the first-run `/setup` page. Sees the **All stores** page, can view any store's Stock and History from a dropdown, download Excel for one store or all stores, and use **Accounts** to deactivate/reactivate a store account or reset its password. Deactivating hides the store from reports and blocks its login, but keeps its history. The owner does not scan.
- **Store account**: signs up at `/signup` with a store name, username, password and the sign-up code. Sees only its own Scan, Stock, History, Drug list and Account (change password, rename store). Asking for another store (`?store=...`) is refused.
- **Your name**: an optional box on the Scan page, remembered in the browser. It is saved in each change's note as `By <name>` and shown in History.

Older databases are upgraded on startup: `admin` becomes `owner`, `staff` and `manager` become `store`. No tables or rows are removed.

### Settings (environment variables)
| Name | What it does |
|---|---|
| `DATABASE_URL` | Neon connection string (without it, a local `inventory.db` is used) |
| `SECRET_KEY` | Signs login cookies |
| `SIGNUP_CODE` | The code a new store must enter to sign up. If it is not set, sign-up is turned off |

### Security
CSRF token on every POST, hashed passwords, HTTPS-only cookies on Render, and a rate limit on login and sign-up (5 failed attempts from one IP in 15 minutes blocks that IP for 15 minutes; kept in memory, per server process).

## Count day
Lets several people count the same store at once without overwriting each other.

- **Count day** page (store accounts, and the owner for any store): **Start count**. While a count is open, every scan on the Scan page *adds* to a tally (name, optional location). Stock does not change until the count is finalized.
- While a count is open, receiving and dispensing are blocked for that store (the API enforces it too).
- Everyone can **Undo my last count** (only their own). The review page shows counted vs. on hand per drug, who counted, locations and every entry (entries can be edited or deleted; each change is logged), plus drugs in stock that were not counted.
- **Finalize** applies the count in one all-or-nothing database transaction as "adjusted" transactions (`Count day <date>: counted 8 (was 20) | By Priyanka, Mike`). Optionally set uncounted drugs to 0. Finalizing twice does nothing. **Cancel** leaves stock unchanged and keeps the entries.
- Only one open count per store (enforced by a unique partial index). New tables: `count_sessions`, `count_entries`.
- **Download** page (`/export`, in the nav; the Stock, History and All stores buttons go here): pick a period (This year by default, Last year, Last 30 days, or custom dates) and, for the owner, a store or "All stores". The Excel report has an **Inventory count** sheet (store name, "as of" date, who counted, total value, a print-ready table with a sign-off line) and a **Scan log** sheet; with a finalized count in the period it also has a **Count entries** sheet. The owner's all-stores file starts with a **Summary** sheet, then "<Store> count" and "<Store> log" for each store. `/export/stock` now just redirects to this report.

## Tests
    python test_accounts.py
    python test_counts.py
    python test_dates.py
    python test_export.py
Runs against a throwaway local SQLite file, never Neon.

## Hosting on Render
See the step-by-step guide in the chat, or render.com/docs. Settings are in `render.yaml`.

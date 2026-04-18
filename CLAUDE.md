# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Script

```bash
uv run --with anthropic --with beautifulsoup4 python analyze.py
```

Auto-loads `.env` from the project root — no need to export env vars manually.

## Architecture

Single-file pipeline: `analyze.py` → `output/transactions.csv` + `output/report.html`

**Data flow:**
1. Parse 4 sources from `Takeout/Google Pay/` into a unified list of dicts
2. **One Claude API call** (Haiku): batch-categorize ~450 unique merchant names → 13 categories
3. Write `output/transactions.csv` (all records, sorted by date desc)
4. Write `output/report.html` — self-contained, Chart.js via CDN, all chart data inline

## Key Parsing Details

- **`My Activity/My Activity.html`** (~1.3M tokens) — main UPI source (~4,040 transactions). Parsed with regex on raw HTML (not BeautifulSoup — file too large). Each block matched via `class="content-cell[^"]*mdl-typography--body-1"`. Timestamps use `\u202f` (narrow no-break space) before AM/PM — normalized before `strptime`. Status checked by scanning 800 chars ahead in raw HTML for "Completed".
- **`Google transactions/*.csv`** — Play Store / YouTube purchases; filtered to `Complete` status only.
- **`Cashback Rewards.csv`** — stored as negative `amount_inr` (money in).
- **`Group expenses/Group expenses.json`** — only records where payer matches "jatin sharma".

**Record schema** (shared across all sources):
```
date, merchant, amount_inr, category, type, payment_method, raw_description
```

## HTML Report

3 charts side-by-side (no scrolling): Monthly Spend Trend (line), Spend by Category (doughnut, top 10 + Other), Top 10 Merchants (horizontal bar, excludes "Unknown (P2P)").

- Month axis labels: `MMM-YY` format (e.g. `Apr-26`)
- Header dates: `dd-MMM-yy` format (e.g. `18-Apr-26`)
- Stats: Total Spent (in Lakhs), Transactions count, Avg/Month (in thousands)
- Category donut: top 10 shown, remainder collapsed into "Other" to avoid visual noise

## Environment

```
ANTHROPIC_API_KEY=...   # required
PAYER_NAME=your name    # used to filter group expenses by payer; if unset, group expenses are skipped
```

Loaded via `python-dotenv` (`load_dotenv()`). Model used: `claude-haiku-4-5-20251001` (merchant categorization only).

## Security Notes

- `safe_json()` wraps all `json.dumps()` calls for chart data embedded in `<script>` blocks — escapes `</` → `<\/` to prevent script injection from malicious merchant names in Takeout data.
- `PAYER_NAME` is read from env, not hardcoded, to avoid embedding PII in source.
- `.gitignore` excludes `.env`, `Takeout/`, and `output/` — no secrets or personal data in the repo.

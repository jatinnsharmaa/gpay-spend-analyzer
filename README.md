# GPay Spend Optimizer

Turns your Google Pay transaction history into a visual spending dashboard — one script, no database, no server.

## What it does

- Parses your Google Takeout export (UPI payments, Play Store purchases, group expenses, cashback)
- Categorizes ~450 unique merchants via Claude API (Haiku)
- Outputs a clean `transactions.csv` and a self-contained `report.html` dashboard

## Dashboard includes

- Monthly spend trend (last 24 months)
- Spend by category — top 10, rest collapsed into Other
- Top 10 merchants by total spend

## Setup

1. Export your data from [takeout.google.com](https://takeout.google.com) → select Google Pay → save to this folder as `Takeout/`
2. Add your Anthropic API key to `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
3. Run:
   ```bash
   uv run --with anthropic --with beautifulsoup4 --with python-dotenv python analyze.py
   ```
4. Open `output/report.html` in your browser

## Output

| File | Description |
|---|---|
| `output/transactions.csv` | All completed transactions, normalized and categorized |
| `output/report.html` | Self-contained visual dashboard, no internet required |

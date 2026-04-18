#!/usr/bin/env python3
"""GPay Spend Optimizer — parses Google Takeout data and generates CSV + HTML report."""

import re
import json
import csv
import os
import sys
from datetime import datetime
from collections import defaultdict
from pathlib import Path

import anthropic
from bs4 import BeautifulSoup

TAKEOUT_DIR = Path(__file__).parent / "Takeout" / "Google Pay"
OUTPUT_DIR = Path(__file__).parent / "output"

# Set via PAYER_NAME env var to avoid embedding PII in source
PAYER_NAME = os.environ.get("PAYER_NAME", "").lower()


def safe_json(obj) -> str:
    """JSON serializer safe for inline <script> blocks — escapes </ to prevent script injection."""
    return json.dumps(obj, ensure_ascii=True).replace("</", "<\\/")

CATEGORIES = [
    "Food & Dining", "Groceries", "Transport", "Subscriptions",
    "Investments", "Telecom", "Entertainment", "Health & Wellness",
    "Shopping", "P2P Transfer", "Travel", "Education", "Other"
]


# ---------------------------------------------------------------------------
# PARSERS
# ---------------------------------------------------------------------------

def parse_my_activity(html_path: Path) -> list[dict]:
    """Parse My Activity.html — the main UPI transaction source."""
    print("Parsing My Activity.html...")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    records = []

    # Use regex directly on raw HTML — much faster and reliable for this structure
    # Each transaction block: content-cell div with payment text + timestamp
    # Pattern: extract all content-cell blocks that contain payment info
    block_pattern = re.compile(
        r'class="content-cell[^"]*mdl-typography--body-1">(.*?)</div>',
        re.DOTALL
    )

    for match in block_pattern.finditer(content):
        block_html = match.group(1)
        # Strip HTML tags to get text
        text = re.sub(r'<[^>]+>', '\n', block_html)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if not lines:
            continue

        first = lines[0]

        # Only process Paid / Sent — skip Received
        if not (first.startswith("Paid ") or first.startswith("Sent ")):
            continue

        # Extract amount
        amt_match = re.search(r"₹([\d,]+\.?\d*)", first)
        if not amt_match:
            continue
        amount = float(amt_match.group(1).replace(",", ""))

        # Extract merchant / recipient
        if first.startswith("Paid") and " to " in first:
            merchant = re.sub(r"\s+using Bank.*$", "", first.split(" to ", 1)[1]).strip()
        else:
            merchant = "Unknown (P2P)"

        # Extract date — look for timestamp pattern in lines
        # Note: timestamps use \u202f (narrow no-break space) before AM/PM
        date_str = None
        for line in lines[1:4]:
            normalized = line.replace("\u202f", " ")
            dt_match = re.search(r"(\w+ \d+, \d{4}, \d+:\d+:\d+ [AP]M)", normalized)
            if dt_match:
                try:
                    date_str = datetime.strptime(
                        dt_match.group(1), "%b %d, %Y, %I:%M:%S %p"
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    pass
                break

        if not date_str:
            continue

        # Check status — look ahead for "Completed" within next 800 chars
        pos = match.end()
        context = content[pos:pos+800]
        if "Completed" not in context:
            continue

        # Payment method
        pm_match = re.search(r"using (Bank Account[^\n<]*|UPI[^\n<]*|Credit Card[^\n<]*|Debit Card[^\n<]*)", first)
        payment_method = pm_match.group(1).strip() if pm_match else "UPI"

        records.append({
            "date": date_str,
            "merchant": merchant,
            "amount_inr": amount,
            "type": "UPI",
            "payment_method": payment_method,
            "raw_description": first,
        })

    print(f"  → {len(records)} UPI transactions")
    return records


def parse_google_transactions(csv_path: Path) -> list[dict]:
    """Parse Google Play / YouTube transactions CSV."""
    print("Parsing Google transactions CSV...")
    records = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Status", "").strip() != "Complete":
                continue
            amt_str = row.get("Amount", "0").replace("INR", "").replace(",", "").strip()
            try:
                amount = float(amt_str)
            except ValueError:
                continue
            if amount == 0:
                continue

            try:
                date_str = datetime.strptime(
                    row["Time"].strip(), "%b %d, %Y, %I:%M %p"
                ).strftime("%Y-%m-%d")
            except ValueError:
                try:
                    date_str = datetime.strptime(
                        row["Time"].strip(), "%b %d, %Y, %I:%M:%S %p"
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    continue

            merchant = row.get("Description", "").strip()
            product = row.get("Product", "").strip()
            if product:
                merchant = f"{merchant} ({product})"

            records.append({
                "date": date_str,
                "merchant": merchant,
                "amount_inr": amount,
                "type": "Play/YouTube",
                "payment_method": row.get("Payment method", "").strip(),
                "raw_description": row.get("Description", ""),
            })

    print(f"  → {len(records)} Play/YouTube transactions")
    return records


def parse_group_expenses(json_path: Path) -> list[dict]:
    """Parse Group expenses JSON — extract what the user personally paid."""
    print("Parsing Group expenses...")
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    records = []
    for group in data.get("Group_expenses", []):
        if group.get("state") not in ("COMPLETED", "CLOSED"):
            continue
        for item in group.get("items", []):
            if not PAYER_NAME or item.get("payer", "").lower() != PAYER_NAME:
                continue
            if item.get("state") != "PAID_RECEIVED":
                continue
            amt_str = re.sub(r"[^\d.]", "", item.get("amount", "0").replace(",", ""))
            try:
                amount = float(amt_str)
            except ValueError:
                continue
            date_str = group.get("creation_time", "")[:10]
            records.append({
                "date": date_str,
                "merchant": f"Group: {group.get('group_name', 'expense')}",
                "amount_inr": amount,
                "type": "Group Expense",
                "payment_method": "UPI",
                "raw_description": group.get("title", group.get("group_name", "")),
            })

    print(f"  → {len(records)} group expense records")
    return records


def parse_cashback(csv_path: Path) -> list[dict]:
    """Parse Cashback Rewards CSV."""
    print("Parsing Cashback Rewards...")
    records = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                amount = float(row.get("Reward Amount", 0))
                date_str = row["Date"][:10]
            except (ValueError, KeyError):
                continue
            records.append({
                "date": date_str,
                "merchant": "Google Pay Cashback",
                "amount_inr": -amount,  # negative = money received
                "type": "Cashback",
                "payment_method": "Cashback",
                "raw_description": row.get("Rewards Description", ""),
            })
    print(f"  → {len(records)} cashback records")
    return records


# ---------------------------------------------------------------------------
# LLM CALLS
# ---------------------------------------------------------------------------

def categorize_merchants(records: list[dict], client: anthropic.Anthropic) -> list[dict]:
    """LLM Call 1: categorize unique merchant names."""
    print("\nCategorizing merchants via Claude API...")

    # Deduplicate
    unique_merchants = list({r["merchant"] for r in records if r["amount_inr"] > 0})
    print(f"  → {len(unique_merchants)} unique merchants to categorize")

    categories_str = ", ".join(CATEGORIES)

    # Batch in chunks of 200
    merchant_to_category = {}
    chunk_size = 200
    for i in range(0, len(unique_merchants), chunk_size):
        chunk = unique_merchants[i:i + chunk_size]
        merchant_list = "\n".join(f"- {m}" for m in chunk)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": f"""Categorize each merchant/payee below into exactly one of these categories:
{categories_str}

Rules:
- Zerodha, BSE, mutual funds → Investments
- Swiggy, Zomato, restaurants → Food & Dining
- Swiggy Instamart, Blinkit, BigBasket → Groceries
- Uber, Ola, metro, cab → Transport
- Spotify, YouTube, Netflix, Play Store apps → Subscriptions
- Airtel, Jio, telecom → Telecom
- MakeMyTrip, Cleartrip, ixigo, flight/hotel → Travel
- Amazon, Myntra, Lifestyle, shopping → Shopping
- Doctors, hospitals, pharmacies, health apps → Health & Wellness
- BOLD Education, courses, books → Education
- Person names (domestic help, friends) → P2P Transfer
- Unknown or "Unknown (P2P)" → P2P Transfer

Return ONLY a JSON object mapping each merchant name to its category. No explanation.

Merchants:
{merchant_list}"""
            }]
        )

        try:
            result = json.loads(response.content[0].text)
            merchant_to_category.update(result)
        except json.JSONDecodeError:
            # Fallback: try to extract JSON from response
            text = response.content[0].text
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                    merchant_to_category.update(result)
                except json.JSONDecodeError:
                    pass

        print(f"  → Categorized batch {i // chunk_size + 1}")

    # Apply categories
    for r in records:
        if r["type"] == "Cashback":
            r["category"] = "Cashback"
        elif r["type"] == "Group Expense":
            r["category"] = "Food & Dining"
        else:
            r["category"] = merchant_to_category.get(r["merchant"], "Other")

    return records


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

def write_csv(records: list[dict], output_path: Path):
    """Write normalized transactions CSV."""
    print(f"\nWriting {output_path}...")
    fieldnames = ["date", "merchant", "amount_inr", "category", "type", "payment_method"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in sorted(records, key=lambda x: x["date"], reverse=True):
            writer.writerow({k: r.get(k, "") for k in fieldnames})
    print(f"  → {len(records)} rows written")


def write_html(records: list[dict], output_path: Path):
    """Write self-contained HTML report."""
    print(f"Writing {output_path}...")

    spend_records = [r for r in records if r["amount_inr"] > 0 and r["type"] != "Cashback"]
    total_spend = sum(r["amount_inr"] for r in spend_records)
    date_range = f"{min(r['date'] for r in spend_records)} to {max(r['date'] for r in spend_records)}"

    # Monthly totals — format as MMM-YY
    monthly_totals = defaultdict(float)
    for r in spend_records:
        monthly_totals[r["date"][:7]] += r["amount_inr"]
    sorted_months = sorted(monthly_totals.keys())[-24:]

    def fmt_month(ym: str) -> str:
        return datetime.strptime(ym, "%Y-%m").strftime("%b-%y")

    monthly_labels = safe_json([fmt_month(m) for m in sorted_months])
    monthly_data = safe_json([round(monthly_totals[m], 2) for m in sorted_months])

    # Category totals
    cat_totals = defaultdict(float)
    for r in spend_records:
        cat_totals[r["category"]] += r["amount_inr"]
    cat_sorted = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
    cat_labels = safe_json([c[0] for c in cat_sorted])
    cat_data = safe_json([round(c[1], 2) for c in cat_sorted])

    # Top 10 merchants
    merchant_totals = defaultdict(float)
    for r in spend_records:
        merchant_totals[r["merchant"]] += r["amount_inr"]
    top10 = sorted(merchant_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    merch_labels = safe_json([m[0][:30] for m in top10])
    merch_data = safe_json([round(m[1], 2) for m in top10])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GPay Spend Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; color: #1a1a2e; }}
  .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); color: white; padding: 40px 32px; }}
  .header h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 8px; }}
  .header p {{ opacity: 0.7; font-size: 14px; }}
  .stats-bar {{ display: flex; gap: 16px; margin-top: 24px; flex-wrap: wrap; }}
  .stat {{ background: rgba(255,255,255,0.1); border-radius: 12px; padding: 16px 20px; flex: 1; min-width: 140px; }}
  .stat .val {{ font-size: 22px; font-weight: 700; }}
  .stat .lbl {{ font-size: 12px; opacity: 0.7; margin-top: 4px; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px 16px; }}
  .section-title {{ font-size: 18px; font-weight: 600; margin: 32px 0 16px; color: #1a1a2e; }}
  .charts-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(480px, 1fr)); gap: 20px; }}
  .card {{ background: white; border-radius: 16px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
  .card h3 {{ font-size: 14px; font-weight: 600; color: #666; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 20px; }}
  .chart-wrap {{ position: relative; height: 280px; }}
  @media (max-width: 600px) {{ .charts-grid {{ grid-template-columns: 1fr; }} .stat .val {{ font-size: 18px; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>💳 GPay Spend Report</h1>
  <p>{date_range} &nbsp;•&nbsp; Generated {datetime.now().strftime('%b %d, %Y')}</p>
  <div class="stats-bar">
    <div class="stat"><div class="val">₹{total_spend:,.0f}</div><div class="lbl">Total Spent</div></div>
    <div class="stat"><div class="val">{len(spend_records):,}</div><div class="lbl">Transactions</div></div>
    <div class="stat"><div class="val">₹{total_spend/max(len(sorted_months),1):,.0f}</div><div class="lbl">Avg/Month</div></div>
  </div>
</div>

<div class="container">
  <div class="section-title">Spending Overview</div>
  <div class="charts-grid">
    <div class="card">
      <h3>Monthly Spend Trend</h3>
      <div class="chart-wrap"><canvas id="monthlyChart"></canvas></div>
    </div>
    <div class="card">
      <h3>Spend by Category</h3>
      <div class="chart-wrap"><canvas id="categoryChart"></canvas></div>
    </div>
    <div class="card">
      <h3>Top 10 Merchants</h3>
      <div class="chart-wrap"><canvas id="merchantChart"></canvas></div>
    </div>
  </div>
</div>

<script>
const COLORS = ['#0f3460','#533483','#e94560','#f5a623','#2ecc71','#3498db','#9b59b6','#e67e22','#1abc9c','#e74c3c','#95a5a6','#34495e','#27ae60'];

new Chart(document.getElementById('monthlyChart'), {{
  type: 'line',
  data: {{ labels: {monthly_labels}, datasets: [{{ label: 'Spend (₹)', data: {monthly_data}, borderColor: '#0f3460', backgroundColor: 'rgba(15,52,96,0.08)', tension: 0.4, fill: true, pointRadius: 3 }}] }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ ticks: {{ callback: v => '₹' + (v/1000).toFixed(0) + 'k' }} }} }} }}
}});

new Chart(document.getElementById('categoryChart'), {{
  type: 'doughnut',
  data: {{ labels: {cat_labels}, datasets: [{{ data: {cat_data}, backgroundColor: COLORS }}] }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ position: 'right', labels: {{ font: {{ size: 11 }} }} }} }} }}
}});

new Chart(document.getElementById('merchantChart'), {{
  type: 'bar',
  data: {{ labels: {merch_labels}, datasets: [{{ data: {merch_data}, backgroundColor: '#0f3460' }}] }},
  options: {{ indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ ticks: {{ callback: v => '₹' + (v/1000).toFixed(0) + 'k' }} }} }} }}
}});

</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  → report.html written")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Parse all sources
    records = []
    records += parse_my_activity(TAKEOUT_DIR / "My Activity" / "My Activity.html")
    records += parse_google_transactions(TAKEOUT_DIR / "Google transactions" / "transactions_774561615601.csv")
    records += parse_group_expenses(TAKEOUT_DIR / "Group expenses" / "Group expenses.json")
    records += parse_cashback(TAKEOUT_DIR / "Rewards earned" / "Cashback Rewards.csv")

    print(f"\nTotal records: {len(records)}")

    # Categorize
    records = categorize_merchants(records, client)

    # Write outputs
    write_csv(records, OUTPUT_DIR / "transactions.csv")
    write_html(records, OUTPUT_DIR / "report.html")

    print(f"\nDone! Open output/report.html in your browser.")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        # fallback if python-dotenv not installed
        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    main()

#!/usr/bin/env python3
"""Expense Tracker — Query & Insight Engine

Reads the canonical `workspace/finance/transactions.csv` and produces markdown
tables for agent consumption (budget protocol generation, monthly summaries).

Subcommands:

    monthly <YYYY-MM>        Category breakdown + top merchants for one month.
    range <YYYY-MM> <YYYY-MM>  Same, over a date range.
    category <name>          Dump all rows matching a spend_category.
    uncategorized            List rows where spend_category is "Uncategorized".
    summary                  All-time per-category totals (descending amount).
    trading                  BUY / SELL activity with ticker, quantity, price.
    transfers                Peer to Peer Transfer + Girlfriend & Partner rows.
    top <N>                  Top-N merchants by total absolute amount (all time).
    export-monthly <dir>     Write one .md summary per month into <dir>.

All subcommands output markdown to stdout (except `export-monthly`).
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CANONICAL_CSV = ROOT / "workspace" / "finance" / "transactions.csv"

# ── helpers ──────────────────────────────────────────────────────────────────

def _load():
    with open(CANONICAL_CSV, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def _safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0

def _net_spend_amount(row):
    """Net spend amount: spending is negative in CSV, refunds are positive.
    Returns -amount so spending becomes positive and refunds subtract from totals."""
    return -_safe_float(row.get("amount", 0))

def _md_table(headers, rows):
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
    sep = "|" + "|".join("-" * (w + 2) for w in col_widths) + "|"
    header_line = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, col_widths)) + " |"
    out = [header_line, sep]
    for row in rows:
        out.append("| " + " | ".join(str(c).ljust(w) for c, w in zip(row, col_widths)) + " |")
    return "\n".join(out)

# ── filters ──────────────────────────────────────────────────────────────────

def _in_range(row, start, end, field="date"):
    return start <= row.get(field, "")[:7] <= end

def _spend_rows(rows):
    types = {"CARD_TRANSACTION", "CARD_TRANSACTION_INTERNATIONAL",
             "TRANSFER_INSTANT_OUTBOUND"}
    return [r for r in rows if r.get("type") in types]

def _trading_rows(rows):
    trading_types = {"BUY", "SELL", "BONUS", "FREE_RECEIPT", "BENEFITS_SAVEBACK",
                     "PRIVATE_MARKET_BUY"}
    return [r for r in rows if r.get("type") in trading_types]

# ── subcommands ──────────────────────────────────────────────────────────────

def cmd_monthly(ym):
    rows = _load()
    spend_rows = [r for r in _spend_rows(rows) if _in_range(r, ym, ym)]

    if not spend_rows:
        print(f"No spending transactions found for {ym}.")
        return

    # category totals
    cat_total: dict[str, float] = defaultdict(float)
    cat_count: dict[str, int] = defaultdict(int)
    merchant_total: dict[str, float] = defaultdict(float)

    for r in spend_rows:
        cat = r.get("spend_category", "Uncategorized")
        amt = _net_spend_amount(r)
        cat_total[cat] += amt
        cat_count[cat] += 1

        merchant = r.get("counterparty_name", "").strip() or r.get("description", "").strip()
        merchant = merchant.replace("null", "").strip()
        if merchant:
            merchant_total[merchant] += amt

    # Drop net-negative categories (refunds exceeding charges)
    cat_total = {k: v for k, v in cat_total.items() if v > 0}
    merchant_total = {k: v for k, v in merchant_total.items() if v > 0}

    total = sum(cat_total.values())

    print(f"## Monthly Spending — {ym}\n")
    print(f"**Total spending:** €{total:,.2f}  |  **Transactions:** {len(spend_rows)}\n")

    sorted_cats = sorted(cat_total.items(), key=lambda x: -x[1])
    rows_table = []
    for cat, amt in sorted_cats:
        pct = (amt / total * 100) if total else 0
        rows_table.append([cat, f"€{amt:,.2f}", f"{pct:.1f}%", str(cat_count[cat])])
    print(_md_table(["Category", "Amount", "% of Total", "# Txns"], rows_table))

    # top merchants
    sorted_merch = sorted(merchant_total.items(), key=lambda x: -x[1])[:15]
    if sorted_merch:
        print("\n### Top Merchants\n")
        mrows = [[m, f"€{v:,.2f}"] for m, v in sorted_merch]
        print(_md_table(["Merchant", "Total"], mrows))

    print()


def cmd_range(start, end):
    rows = _load()
    spend_rows = [r for r in _spend_rows(rows) if _in_range(r, start, end)]

    if not spend_rows:
        print(f"No spending transactions found for {start} → {end}.")
        return

    cat_total: dict[str, float] = defaultdict(float)
    for r in spend_rows:
        cat = r.get("spend_category", "Uncategorized")
        amt = _net_spend_amount(r)
        cat_total[cat] += amt

    cat_total = {k: v for k, v in cat_total.items() if v > 0}
    total = sum(cat_total.values())
    months = len(set(r.get("date", "")[:7] for r in spend_rows))

    print(f"## Spending Summary — {start} → {end}\n")
    print(f"**Total:** €{total:,.2f}  |  **Months:** {months}  |  "
          f"**Avg/month:** €{total / months:,.2f}  |  **Transactions:** {len(spend_rows)}\n")

    sorted_cats = sorted(cat_total.items(), key=lambda x: -x[1])
    table_rows = []
    for cat, amt in sorted_cats:
        pct = (amt / total * 100) if total else 0
        avg = amt / months if months else 0
        table_rows.append([cat, f"€{amt:,.2f}", f"{pct:.1f}%",
                           f"€{avg:,.2f}"])
    print(_md_table(["Category", "Total", "% of Total", "Avg/Month"], table_rows))
    print()


def cmd_category(name):
    rows = _load()
    matches = [r for r in rows if r.get("spend_category", "").strip().lower()
               == name.lower()]
    if not matches:
        print(f"No transactions found for category '{name}'.")
        return

    total = 0.0
    table_rows = []
    for r in sorted(matches, key=lambda x: x.get("date", "")):
        raw = _safe_float(r.get("amount", 0))
        total += _net_spend_amount(r) if r.get("type") in _spend_rows([r]) else abs(raw)
        merchant = r.get("counterparty_name", "").strip() or r.get("description", "").strip()
        merchant = merchant.replace("null", "").strip()
        table_rows.append([
            r.get("date", ""),
            merchant,
            f"€{raw:,.2f}",
            r.get("type", ""),
            r.get("transaction_id", ""),
        ])

    print(f"## Category: {name}\n")
    print(f"**Total:** €{total:,.2f}  |  **Transactions:** {len(matches)}\n")
    print(_md_table(["Date", "Merchant/Description", "Amount", "Type", "ID"], table_rows))
    print()


def cmd_uncategorized():
    rows = _load()
    matches = [r for r in rows if r.get("spend_category", "").strip() == "Uncategorized"]
    if not matches:
        print("No uncategorized transactions.")
        return

    table_rows = []
    for r in sorted(matches, key=lambda x: x.get("date", "")):
        merchant = r.get("counterparty_name", "").strip() or r.get("description", "").strip()
        table_rows.append([
            r.get("date", ""),
            f"€{_safe_float(r.get('amount', 0)):,.2f}",
            r.get("type", ""),
            r.get("mcc_code", ""),
            merchant,
            r.get("transaction_id", ""),
        ])

    print(f"## Uncategorized Transactions ({len(matches)})\n")
    print(_md_table(["Date", "Amount", "Type", "MCC", "Description", "ID"], table_rows))
    print()


def cmd_summary():
    rows = _load()
    spend_rows = _spend_rows(rows)
    cat_total: dict[str, float] = defaultdict(float)
    cat_count: dict[str, int] = defaultdict(int)

    for r in spend_rows:
        cat = r.get("spend_category", "Uncategorized")
        amt = _net_spend_amount(r)
        cat_total[cat] += amt
        cat_count[cat] += 1

    cat_total = {k: v for k, v in cat_total.items() if v > 0}
    total = sum(cat_total.values())

    print("## All-Time Spending by Category\n")
    print(f"**Total tracked:** €{total:,.2f}  |  **Transactions:** {len(rows)}\n")

    sorted_cats = sorted(cat_total.items(), key=lambda x: -x[1])
    table_rows = []
    for cat, amt in sorted_cats:
        pct = (amt / total * 100) if total else 0
        table_rows.append([cat, f"€{amt:,.2f}", f"{pct:.1f}%", str(cat_count[cat])])
    print(_md_table(["Category", "Total", "% of Total", "# Txns"], table_rows))
    print()


def cmd_trading():
    rows = _load()
    trades = _trading_rows(rows)
    if not trades:
        print("No trading activity found.")
        return

    table_rows = []
    total_buys = 0.0
    total_sells = 0.0
    total_fees = 0.0

    for r in sorted(trades, key=lambda x: x.get("date", "")):
        amt = _safe_float(r.get("amount", 0))
        fee = abs(_safe_float(r.get("fee", 0)))
        ttype = r.get("type", "")
        if ttype == "BUY":
            total_buys += abs(amt)
        elif ttype == "SELL":
            total_sells += amt
        total_fees += fee

        table_rows.append([
            r.get("date", ""),
            r.get("type", ""),
            r.get("name", ""),
            r.get("symbol", ""),
            r.get("shares", ""),
            f"€{amt:,.2f}",
            f"€{fee:,.2f}",
        ])

    print("## Trading Activity\n")
    print(f"**Total bought:** €{total_buys:,.2f}  |  "
          f"**Total sold:** €{total_sells:,.2f}  |  "
          f"**Total fees:** €{total_fees:,.2f}  |  "
          f"**Trades:** {len(trades)}\n")
    print(_md_table(["Date", "Type", "Asset", "Symbol", "Qty", "Amount", "Fee"],
                     table_rows))
    print()


def cmd_transfers():
    rows = _load()
    transfer_cats = {"Peer to Peer Transfer", "Girlfriend & Partner",
                     "Inbound Transfer", "Investment Platform"}
    matches = [r for r in rows
               if r.get("spend_category", "") in transfer_cats]

    if not matches:
        print("No peer/inbound transfers found.")
        return

    cat_total: dict[str, float] = defaultdict(float)
    table_rows = []
    for r in sorted(matches, key=lambda x: x.get("date", "")):
        amt = _safe_float(r.get("amount", 0))
        cat = r.get("spend_category", "")
        cat_total[cat] += _net_spend_amount(r) if r.get("type") in {"TRANSFER_INSTANT_OUTBOUND"} else abs(amt)
        counterparty = r.get("counterparty_name", "").strip()
        desc = r.get("description", "").strip()
        table_rows.append([
            r.get("date", ""),
            cat,
            f"€{amt:,.2f}",
            counterparty or desc.replace("null", "").strip(),
        ])

    print("## Transfers Overview\n")
    for cat, tot in sorted(cat_total.items(), key=lambda x: -x[1]):
        print(f"  **{cat}:** €{tot:,.2f}")
    print(f"\n  **Total transfers:** {len(matches)}\n")
    print(_md_table(["Date", "Category", "Amount", "Counterparty"], table_rows))
    print()


def cmd_top(n=15):
    rows = _load()
    spend_rows = _spend_rows(rows)
    merchant_total: dict[str, float] = defaultdict(float)
    for r in spend_rows:
        amt = _net_spend_amount(r)
        merchant = r.get("counterparty_name", "").strip() or r.get("description", "").strip()
        merchant = merchant.replace("null", "").strip()
        if merchant:
            merchant_total[merchant] += amt

    merchant_total = {k: v for k, v in merchant_total.items() if v > 0}
    sorted_merch = sorted(merchant_total.items(), key=lambda x: -x[1])[:n]

    print(f"## Top {n} Merchants by Total Amount\n")
    table_rows = [[m, f"€{v:,.2f}"] for m, v in sorted_merch]
    print(_md_table(["Merchant", "Total Amount"], table_rows))
    print()


def cmd_export_monthly(dir_path):
    rows = _load()
    months = sorted(set(r.get("date", "")[:7] for r in rows if r.get("date")))

    out_dir = Path(dir_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    for ym in months:
        spend_rows = [r for r in _spend_rows(rows) if _in_range(r, ym, ym)]
        if not spend_rows:
            continue

        cat_total: dict[str, float] = defaultdict(float)
        cat_count: dict[str, int] = defaultdict(int)
        merchant_total: dict[str, float] = defaultdict(float)

        for r in spend_rows:
            cat = r.get("spend_category", "Uncategorized")
            amt = _net_spend_amount(r)
            cat_total[cat] += amt
            cat_count[cat] += 1
            merchant = r.get("counterparty_name", "").strip() or r.get("description", "").strip()
            merchant = merchant.replace("null", "").strip()
            if merchant:
                merchant_total[merchant] += amt

        cat_total = {k: v for k, v in cat_total.items() if v > 0}
        merchant_total = {k: v for k, v in merchant_total.items() if v > 0}
        total = sum(cat_total.values())

        lines = []
        lines.append(f"# Monthly Spending — {ym}\n")
        lines.append(f"**Total:** €{total:,.2f}  |  "
                     f"**Transactions:** {len(spend_rows)}\n")

        sorted_cats = sorted(cat_total.items(), key=lambda x: -x[1])
        lines.append("| Category | Amount | % of Total | # Txns |")
        lines.append("|---|---|---|---|")
        for cat, amt in sorted_cats:
            pct = (amt / total * 100) if total else 0
            lines.append(f"| {cat} | €{amt:,.2f} | {pct:.1f}% | {cat_count[cat]} |")
        lines.append("")

        sorted_merch = sorted(merchant_total.items(), key=lambda x: -x[1])[:10]
        if sorted_merch:
            lines.append("## Top Merchants\n")
            lines.append("| Merchant | Total |")
            lines.append("|---|---|")
            for m, v in sorted_merch:
                lines.append(f"| {m} | €{v:,.2f} |")
            lines.append("")

        file_path = out_dir / f"{ym}.md"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    print(f"Exported {len(months)} monthly summaries to {out_dir}/")


# ── CLI dispatch ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "monthly" and len(sys.argv) >= 3:
        cmd_monthly(sys.argv[2])
    elif cmd == "range" and len(sys.argv) >= 4:
        cmd_range(sys.argv[2], sys.argv[3])
    elif cmd == "category" and len(sys.argv) >= 3:
        cmd_category(sys.argv[2])
    elif cmd == "uncategorized":
        cmd_uncategorized()
    elif cmd == "summary":
        cmd_summary()
    elif cmd == "trading":
        cmd_trading()
    elif cmd == "transfers":
        cmd_transfers()
    elif cmd == "top" and len(sys.argv) >= 3:
        cmd_top(int(sys.argv[2]))
    elif cmd == "export-monthly" and len(sys.argv) >= 3:
        cmd_export_monthly(sys.argv[2])
    else:
        print(f"Unknown subcommand: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Expense Tracker — Parse & Merge

Ingests a bank Transaction export CSV from sources/scratch/, deduplicates by
transaction_id, classifies every row by mcc_code or description heuristics,
appends new rows to the canonical `workspace/finance/transactions.csv`.

Usage:
    .venv/bin/python .agents/skills/expense-tracker/scripts/parse.py \
        sources/scratch/Transaction export.csv
"""

import csv
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CANONICAL_CSV = ROOT / "workspace" / "finance" / "transactions.csv"

# ── MCC code → human-readable spend_category ─────────────────────────────────
MCC_MAP: dict[str, str] = {
    # Travel & Transport
    "3246": "Travel & Transport",
    "4111": "Travel & Transport",
    "4511": "Travel & Transport",
    "7011": "Travel & Accommodation",
    # Utilities
    "4900": "Utilities",
    # Home
    "5251": "Home & Hardware",
    # Shopping / General Merchandise
    "5311": "Shopping",
    "5399": "Shopping",
    "5944": "Shopping",
    "5947": "Gifts & Souvenirs",
    "5999": "Other Shopping",
    # Food
    "5411": "Groceries & Supermarkets",
    "5499": "Food & Convenience",
    # Fuel
    "5542": "Fuel & Gas",
    # Clothing
    "5651": "Clothing & Apparel",
    # Electronics
    "5722": "Electronics & Appliances",
    "5732": "Electronics & Appliances",
    "5734": "Software & Subscriptions",
    # Dining & Entertainment
    "5812": "Restaurants & Dining",
    "5813": "Bars & Nightlife",
    "5816": "Entertainment & Gaming",
    "7922": "Entertainment & Events",
    # Sports
    "5941": "Sports & Outdoors",
    # Books
    "5942": "Books & Education",
    # Personal Care
    "7230": "Personal Care",
}

# ── Description / counterparty heuristics for rows without an MCC ─────────────
# User-specific patterns live in workspace/finance/category_rules.json so that
# skill files remain free of personal data (per AGENTS.md).


def _load_rules() -> dict:
    rules_path = ROOT / "workspace" / "finance" / "category_rules.json"
    if not rules_path.exists():
        return {}
    with open(rules_path, "r") as f:
        return json.load(f)


def classify_transaction(mcc_code: str, trans_type: str, description: str,
                         counterparty: str, rules: dict | None = None) -> str:
    """Return a human-readable spend_category for a single row."""
    mcc = (mcc_code or "").strip()

    if mcc and mcc in MCC_MAP:
        return MCC_MAP[mcc]

    if trans_type == "TRANSFER_INSTANT_OUTBOUND":
        if rules is None:
            rules = _load_rules()
        partner_patterns = rules.get("partner_patterns", [])
        haystack = f"{description} {counterparty}".lower()
        for pat in partner_patterns:
            if re.search(pat, haystack, re.IGNORECASE):
                return "Girlfriend & Partner"
        return "Peer to Peer Transfer"

    if trans_type == "CUSTOMER_INBOUND":
        return "Inbound Transfer"

    if description:
        desc_lower = description.lower()
        if any(k in desc_lower for k in ("mintos",)):
            return "Investment Platform"
        if any(k in desc_lower for k in ("binging", "onlyfans", "patreon")):
            return "Adult & Subscriptions"

    return "Uncategorized"


def load_existing_ids() -> set[str]:
    """Return the set of transaction_ids already in the canonical CSV."""
    if not CANONICAL_CSV.exists():
        return set()
    with open(CANONICAL_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row.get("transaction_id", "").strip() for row in reader}


def run(source_path: str) -> dict:
    """Main entry point.  Returns stats dict for the calling agent."""
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"Source CSV not found: {source_path}")

    existing_ids = load_existing_ids()
    existing_columns = set()
    if CANONICAL_CSV.exists():
        with open(CANONICAL_CSV, "r", newline="", encoding="utf-8") as f:
            existing_columns = set(next(csv.reader(f)))

    with open(source, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        source_columns = list(reader.fieldnames or [])
        all_rows = list(reader)

    output_columns = source_columns + ["spend_category"]

    rules = _load_rules()
    new_rows: list[dict] = []
    skipped = 0
    for row in all_rows:
        tid = (row.get("transaction_id") or "").strip()
        if tid in existing_ids:
            skipped += 1
            continue
        row["spend_category"] = classify_transaction(
            mcc_code=row.get("mcc_code", ""),
            trans_type=row.get("type", ""),
            description=row.get("description", ""),
            counterparty=row.get("counterparty_name", ""),
            rules=rules,
        )
        new_rows.append(row)

    if not new_rows:
        return {
            "status": "no_new_transactions",
            "source_rows": len(all_rows),
            "skipped": skipped,
            "new_rows": 0,
            "categories": {},
        }

    file_exists = CANONICAL_CSV.exists()
    with open(CANONICAL_CSV, "a" if file_exists else "w", newline="",
              encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_columns)
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)

    cat_counts: dict[str, int] = {}
    for row in new_rows:
        cat = row.get("spend_category", "Uncategorized")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    return {
        "status": "merged",
        "source_rows": len(all_rows),
        "skipped": skipped,
        "new_rows": len(new_rows),
        "categories": dict(sorted(cat_counts.items(), key=lambda x: -x[1])),
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: parse.py <path/to/bank_export.csv>", file=sys.stderr)
        sys.exit(1)
    import json
    result = run(sys.argv[1])
    print(json.dumps(result, indent=2))

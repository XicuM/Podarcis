"""menumaker-mcp — FastMCP server for nutritional intake and menu optimization.

Provides tools for compute_daily_intake, food DB queries, linear programming menu optimization, and market pricing.
Set PROJECT_ROOT env var to the repository root.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP

# Add parent and self to sys.path
_MENUMAKER_DIR = Path(__file__).resolve().parent
if str(_MENUMAKER_DIR) not in sys.path:
    sys.path.insert(0, str(_MENUMAKER_DIR))

from intake import compute_daily_intake
from food_db import load_food_db, search_foods as db_search_foods, get_food_nutrients as db_get_nutrients
from optimizer import optimize_menu as run_optimizer
from pricing import price_menu as run_pricing

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("MENUMAKER_DATA_DIR", str(_MENUMAKER_DIR / "data"))

def _default_paths() -> tuple[str, str, str]:
    food = os.path.join(DATA_DIR, "food_data", "food_data.csv")
    prices = os.path.join(DATA_DIR, "mercadona.csv")
    recs = os.path.join(DATA_DIR, "recommendations.csv")
    return food, prices, recs

mcp = FastMCP(
    "menumaker-mcp",
    instructions=(
        "Nutritional target calculator, food nutrient search, and menu optimizer server. "
        "Use get_intake_targets for RDA limits, search_foods/get_food_nutrients for food profile queries, "
        "optimize_menu for linear programming cost optimization, and price_menu for market pricing."
    ),
)

@mcp.tool()
def get_intake_targets(
    age: Annotated[int, "Age in years"],
    gender: Annotated[Literal["male", "female"], "Biological gender"],
    stage: Annotated[Literal["adult", "child", "pregnancy", "lactation"], "Life stage"] = "adult",
) -> str:
    """Compute daily nutrient intake targets (Recommended Dietary Allowances and Upper Limits)."""
    result = compute_daily_intake(age, gender, stage)
    profile = result.get("profile", {})
    lines = [
        f"# Daily Intake Targets",
        "",
        f"- **Age**: {profile.get('age')} | **Gender**: {profile.get('gender')} | **Stage**: {profile.get('stage')}",
        "",
        "| Nutrient | Recommended | Tolerable |",
        "|----------|-------------|-----------|",
    ]
    for n in result.get("nutrients", []):
        r = f"{n['recommended']:.2f}" if isinstance(n.get("recommended"), float) else str(n.get("recommended", "-"))
        t = f"{n['tolerable']:.2f}" if isinstance(n.get("tolerable"), float) else str(n.get("tolerable", "-"))
        lines.append(f"| {n['nutrient']} | {r} | {t} |")
    return "\n".join(lines)

@mcp.tool()
def search_foods(
    query: Annotated[str, "Search term for food name (case-insensitive substring match)"],
    limit: Annotated[int, "Maximum results to return"] = 10,
) -> str:
    """Search the USDA food nutrient database by name."""
    food_path, _, _ = _default_paths()
    db = load_food_db(food_path)
    matches = db_search_foods(query, db)
    results = matches.head(limit)
    lines = [f"# Food Search: \"{query}\"", "", f"Found {len(matches)} matches.", ""]
    for food_name in results.index:
        macros = {}
        for col in ["Energy", "Protein", "Carbohydrate, by difference", "Total lipid (fat)"]:
            if col in results.columns:
                v = results.loc[food_name, col]
                if not (isinstance(v, float) and v != v):
                    macros[col] = v
        lines.append(f"## {food_name}")
        for k, v in macros.items():
            lines.append(f"- **{k}**: {v:.2f}" if isinstance(v, float) else f"- **{k}**: {v}")
        lines.append("")
    return "\n".join(lines)

@mcp.tool()
def get_food_nutrients(
    food_name: Annotated[str, "Exact food name from the database"],
) -> str:
    """Get the full nutrient profile for a specific food from the USDA database."""
    food_path, _, _ = _default_paths()
    db = load_food_db(food_path)
    data = db_get_nutrients(food_name, db)
    lines = [f"# {data['name']}", "", "Nutrients per 100g:", ""]
    for k, v in sorted(data["nutrients"].items()):
        display = f"{v:.2f}" if isinstance(v, float) else str(v)
        lines.append(f"- **{k}**: {display}")
    return "\n".join(lines)

@mcp.tool()
def optimize_menu(
    age: Annotated[int, "Age in years"],
    gender: Annotated[Literal["male", "female"], "Biological gender"],
    stage: Annotated[Literal["adult", "child", "pregnancy", "lactation"], "Life stage"] = "adult",
) -> str:
    """Solve for the cheapest combination of foods that satisfies all daily nutrient requirements."""
    food_path, prices_path, _ = _default_paths()
    intake = compute_daily_intake(age, gender, stage)
    result = run_optimizer(food_path, intake, prices_path)
    if "error" in result:
        return f"Error: {result['error']}"
    lines = [
        f"# Optimal Daily Menu",
        "",
        f"**Daily cost**: {result['total_daily_cost_eur']:.4f} EUR | **Monthly estimate**: {result['total_monthly_cost_eur']:.2f} EUR | **Foods**: {result['food_count']}",
        "",
        "## Foods per Day",
        "",
        "| Food | Grams |",
        "|------|-------|",
    ]
    for food, grams in sorted(result.get("menu", {}).items(), key=lambda x: -x[1]):
        lines.append(f"| {food} | {grams:.1f}g |")
    return "\n".join(lines)

@mcp.tool()
def price_menu(
    items: Annotated[dict[str, float], "Dictionary mapping food names to gram amounts"],
) -> str:
    """Calculate the Mercadona and Dia prices for a menu (dict of food names to grams)."""
    _, prices_path, _ = _default_paths()
    result = run_pricing(items, prices_path)
    comp = result.get("comparison", {})
    totals = comp.get("totals", {})
    lines = [
        "# Menu Price Comparison",
        "",
        "| Metric | Mercadona | Dia |",
        "|--------|-----------|-----|",
        f"| **Daily Cost** | {totals['Mercadona']['total_price_eur']:.4f} EUR | {totals['Dia']['total_price_eur']:.4f} EUR |",
        f"| **Monthly Cost** | {totals['Mercadona']['total_monthly_eur']:.2f} EUR | {totals['Dia']['total_monthly_eur']:.2f} EUR |",
    ]
    return "\n".join(lines)

if __name__ == "__main__":
    mcp.run()

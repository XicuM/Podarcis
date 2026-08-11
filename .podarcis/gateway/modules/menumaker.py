'''Menumaker and nutrition module binding for Podarcis MCP Gateway.'''
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Literal

TOOLS = ['get_intake_targets', 'search_foods', 'get_food_nutrients', 'optimize_menu', 'price_menu']

def _get_data_paths(root: Path) -> tuple[str, str, str]:
    data_dir = os.environ.get("MENUMAKER_DATA_DIR", str(root / ".agents" / "mcp" / "data"))
    food = os.path.join(data_dir, "food_data", "food_data.csv")
    prices = os.path.join(data_dir, "mercadona.csv")
    recs = os.path.join(data_dir, "recommendations.csv")
    return food, prices, recs

def register(mcp, root: Path) -> None:
    '''Register menumaker tools with the FastMCP instance.'''
    mcp_dir = root / '.agents' / 'mcp'
    if str(mcp_dir) not in sys.path:
        sys.path.insert(0, str(mcp_dir))

    from menumaker.core.intake import compute_daily_intake
    from menumaker.core.food_db import load_food_db, search_foods as db_search_foods, get_food_nutrients as db_get_nutrients
    from menumaker.core.optimizer import optimize_menu as run_optimizer
    from menumaker.core.pricing import price_menu as run_pricing

    food_path, prices_path, recs_path = _get_data_paths(root)

    @mcp.tool()
    def get_intake_targets(
        age: Annotated[int, "Age in years"],
        gender: Annotated[Literal["male", "female"], "Biological gender"],
        stage: Annotated[Literal["adult", "child", "pregnancy", "lactation"], "Life stage"] = "adult",
    ) -> str:
        '''Compute daily nutrient intake targets (Recommended Dietary Allowances & Upper Limits).'''
        result = compute_daily_intake(age, gender, stage)
        lines = [f"# Daily Intake Targets for {gender}, age {age} ({stage})", "", "| Nutrient | Recommended | Tolerable |", "|----------|-------------|-----------|"]
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
        '''Search the USDA food nutrient database by name.'''
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
        '''Get the full nutrient profile for a specific food from the USDA database.'''
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
        '''Solve for the cheapest combination of foods that satisfies daily nutrient requirements.'''
        intake = compute_daily_intake(age, gender, stage)
        result = run_optimizer(food_path, intake, prices_path)
        if "error" in result:
            return f"Error: {result['error']}"
        lines = [
            f"# Optimal Daily Menu", "",
            f"**Daily cost**: {result['total_daily_cost_eur']:.4f} EUR | **Monthly estimate**: {result['total_monthly_cost_eur']:.2f} EUR",
            "", "| Food | Grams |", "|------|-------|"
        ]
        for food, grams in sorted(result.get("menu", {}).items(), key=lambda x: -x[1]):
            lines.append(f"| {food} | {grams:.1f}g |")
        return "\n".join(lines)

    @mcp.tool()
    def price_menu(
        items: Annotated[dict[str, float], "Dictionary mapping food names to gram amounts"],
    ) -> str:
        '''Calculate Mercadona & Dia market price for a dictionary of food names and gram amounts.'''
        result = run_pricing(items, prices_path)
        comp = result.get("comparison", {})
        totals = comp.get("totals", {})
        lines = [
            "# Menu Price Comparison", "",
            f"- **Mercadona Daily**: {totals['Mercadona']['total_price_eur']:.4f} EUR | **Monthly**: {totals['Mercadona']['total_monthly_eur']:.2f} EUR",
            f"- **Dia Daily**: {totals['Dia']['total_price_eur']:.4f} EUR | **Monthly**: {totals['Dia']['total_monthly_eur']:.2f} EUR"
        ]
        return "\n".join(lines)

def unregister(mcp) -> None:
    '''Unregister menumaker tools from the FastMCP instance.'''
    for tname in TOOLS:
        try:
            mcp.remove_tool(tname)
        except Exception:
            pass

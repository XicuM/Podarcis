"""MCP stdio server exposing menuMaker tools to AI agents.

Usage:
    python -m menumaker.server
    # or:
    python menumaker/server.py
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# Add parent directory to sys.path so 'import menumaker' works
_MCP_DIR = str(Path(__file__).resolve().parent.parent)
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)


from menumaker.core.intake import compute_daily_intake
from menumaker.core.food_db import (
    load_food_db,
    search_foods,
    get_food_nutrients,
)
from menumaker.core.optimizer import optimize_menu as run_optimizer
from menumaker.core.pricing import price_menu as run_pricing

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("MENUMAKER_DATA_DIR", str(Path(__file__).parent.parent / "data"))


def _default_paths() -> tuple[str, str, str]:
    """Return default paths for food data, prices, and recommendations."""
    food = os.path.join(DATA_DIR, "food_data", "food_data.csv")
    prices = os.path.join(DATA_DIR, "mercadona.csv")
    recs = os.path.join(DATA_DIR, "recommendations.csv")
    return food, prices, recs


server = Server("menumaker-mcp")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_intake_targets",
            description=(
                "Compute daily nutrient intake targets (Recommended Dietary Allowances "
                "and Tolerable Upper Limits) for a person based on age, gender, and life stage. "
                "Returns a structured list of nutrients with recommended and tolerable values."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "age": {"type": "integer", "description": "Age in years"},
                    "gender": {
                        "type": "string",
                        "enum": ["male", "female"],
                        "description": "Biological gender",
                    },
                    "stage": {
                        "type": "string",
                        "enum": ["adult", "child", "pregnancy", "lactation"],
                        "description": "Life stage (default: adult)",
                    },
                },
                "required": ["age", "gender"],
            },
        ),
        types.Tool(
            name="search_foods",
            description=(
                "Search the USDA food nutrient database by name. "
                "Returns matching foods with their macro nutrient profiles (per 100g). "
                "Use this to explore what foods are available in the database."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term for food name (case-insensitive substring match)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default: 10)",
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_food_nutrients",
            description=(
                "Get the full nutrient profile for a specific food from the USDA database. "
                "Returns all nutrients per 100g of the food. "
                "Use this to deeply analyze a single food item."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "food_name": {
                        "type": "string",
                        "description": "Exact food name from the database (use search_foods first)",
                    },
                },
                "required": ["food_name"],
            },
        ),

        types.Tool(
            name="optimize_menu",
            description=(
                "Solve for the cheapest combination of foods that satisfies all daily nutrient requirements "
                "using linear programming. Requires intake targets (from get_intake_targets) and a Mercadona "
                "price database for cost data.\n\n"
                "Returns the optimal grams of each food, total daily/monthly cost, and per-nutrient "
                "coverage analysis showing which targets are met, deficient, or exceeded.\n\n"
                "IMPORTANT: This finds the cheapest *commodity* level solution. The agent should translate "
                "these raw ingredients into actual meals considering practicality and taste."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "age": {"type": "integer", "description": "Age in years"},
                    "gender": {
                        "type": "string",
                        "enum": ["male", "female"],
                        "description": "Biological gender",
                    },
                    "stage": {
                        "type": "string",
                        "enum": ["adult", "child", "pregnancy", "lactation"],
                        "description": "Life stage (default: adult)",
                    },
                },
                "required": ["age", "gender"],
            },
        ),
        types.Tool(
            name="price_menu",
            description=(
                "Calculate the Mercadona price for a menu (dict of food names to grams). "
                "Uses fuzzy matching to match USDA food names to Mercadona product names. "
                "Returns per-item price breakdown and total daily/monthly cost."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "object",
                        "description": "Dictionary mapping food names to gram amounts",
                        "additionalProperties": {"type": "number"},
                    },
                },
                "required": ["items"],
            },
        ),

    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    food_path, prices_path, recs_path = _default_paths()

    try:
        if name == "get_intake_targets":
            age = arguments["age"]
            gender = arguments["gender"]
            stage = arguments.get("stage", "adult")
            result = compute_daily_intake(age, gender, stage)
            text = _format_intake_md(result)
            return [types.TextContent(type="text", text=text)]

        elif name == "search_foods":
            query = arguments["query"]
            limit = arguments.get("limit", 10)
            db = load_food_db(food_path)
            matches = search_foods(query, db)
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
            return [types.TextContent(type="text", text="\n".join(lines))]

        elif name == "get_food_nutrients":
            food_name = arguments["food_name"]
            db = load_food_db(food_path)
            data = get_food_nutrients(food_name, db)
            lines = [f"# {data['name']}", "", "Nutrients per 100g:", ""]
            for k, v in sorted(data["nutrients"].items()):
                display = f"{v:.2f}" if isinstance(v, float) else str(v)
                lines.append(f"- **{k}**: {display}")
            return [types.TextContent(type="text", text="\n".join(lines))]



        elif name == "optimize_menu":
            age = arguments["age"]
            gender = arguments["gender"]
            stage = arguments.get("stage", "adult")
            intake = compute_daily_intake(age, gender, stage)
            result = run_optimizer(food_path, intake, prices_path)
            text = _format_optimize_md(result)
            return [types.TextContent(type="text", text=text)]

        elif name == "price_menu":
            items = arguments["items"]
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
                f"| **Matched Foods** | {totals['Mercadona']['matched_count']} | {totals['Dia']['matched_count']} |",
                "",
                "### Detailed Item Comparison",
                "",
                "| Food | Amount | Mercadona Product | Mercadona Price | Dia Product | Dia Price |",
                "|------|--------|-------------------|-----------------|-------------|-----------|",
            ]

            for food, info in comp.get("foods", {}).items():
                m_info = info["providers"].get("Mercadona", {})
                d_info = info["providers"].get("Dia", {})

                m_name = m_info.get("matched_product", "-")
                m_price = f"{m_info.get('price_eur', 0):.4f} EUR" if "error" not in m_info else f"ERR: {m_info['error']}"

                d_name = d_info.get("matched_product", "-")
                d_price = f"{d_info.get('price_eur', 0):.4f} EUR" if "error" not in d_info else f"ERR: {d_info['error']}"

                lines.append(
                    f"| {food} | {info.get('amount_g', 0):.0f}g "
                    f"| {m_name} | {m_price} "
                    f"| {d_name} | {d_price} |"
                )

            if totals["Mercadona"]["unmatched"] or totals["Dia"]["unmatched"]:
                lines.append("\n### Unmatched Foods\n")
                if totals["Mercadona"]["unmatched"]:
                    lines.append("**Mercadona Unmatched:**")
                    for f in totals["Mercadona"]["unmatched"]:
                        lines.append(f"- {f}")
                if totals["Dia"]["unmatched"]:
                    lines.append("\n**Dia Unmatched:**")
                    for f in totals["Dia"]["unmatched"]:
                        lines.append(f"- {f}")

            return [types.TextContent(type="text", text="\n".join(lines))]



        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        logger.exception("Tool error: %s", name)
        return [types.TextContent(type="text", text=f"Error: {e}")]


def _format_intake_md(intake_data: dict) -> str:
    profile = intake_data.get("profile", {})
    lines = [
        f"# Daily Intake Targets",
        "",
        f"- **Age**: {profile.get('age')} | **Gender**: {profile.get('gender')} | **Stage**: {profile.get('stage')}",
        "",
        "| Nutrient | Recommended | Tolerable |",
        "|----------|-------------|-----------|",
    ]
    for n in intake_data.get("nutrients", []):
        r = f"{n['recommended']:.2f}" if isinstance(n.get("recommended"), float) else str(n.get("recommended", "-"))
        t = f"{n['tolerable']:.2f}" if isinstance(n.get("tolerable"), float) else str(n.get("tolerable", "-"))
        lines.append(f"| {n['nutrient']} | {r} | {t} |")
    return "\n".join(lines)


def _format_optimize_md(result: dict) -> str:
    if "error" in result:
        return f"Error: {result['error']}"

    lines = [
        f"# Optimal Daily Menu",
        "",
        f"**Daily cost**: {result['total_daily_cost_eur']:.4f} EUR",
        f"**Monthly estimate**: {result['total_monthly_cost_eur']:.2f} EUR",
        f"**Foods**: {result['food_count']}",
        "",
        "## Foods per Day",
        "",
        "| Food | Grams |",
        "|------|-------|",
    ]
    for food, grams in sorted(result.get("menu", {}).items(), key=lambda x: -x[1]):
        lines.append(f"| {food} | {grams:.1f}g |")

    lines.extend(["", "## Nutrient Coverage", ""])
    lines.append("| Nutrient | Recommended | Tolerable | Actual | Status |")
    lines.append("|----------|-------------|-----------|--------|--------|")
    icons = {"ok": "OK", "deficient": "LOW", "excess": "HIGH"}
    for n in result.get("nutrients", []):
        r = f"{n['recommended']:.2f}" if isinstance(n.get("recommended"), float) else str(n.get("recommended", "-"))
        t = f"{n['tolerable']:.2f}" if isinstance(n.get("tolerable"), float) else str(n.get("tolerable", "-"))
        a = f"{n['actual']:.2f}" if isinstance(n.get("actual"), float) else str(n.get("actual", "-"))
        s = icons.get(n["status"], n["status"])
        lines.append(f"| {n['nutrient']} | {r} | {t} | {a} | {s} |")
    return "\n".join(lines)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        init_opts = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_opts)


def cli():
    """Entry point for the MCP server."""
    import asyncio
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(main())


if __name__ == "__main__":
    cli()

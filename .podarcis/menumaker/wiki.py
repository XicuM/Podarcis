"""Obsidian-compatible markdown formatters for menu data.

All formatters produce markdown suitable for pasting into an Obsidian vault
or writing directly to vault files.
"""

from datetime import datetime
from typing import Any


def _safe(v: Any) -> str:
    """Convert a value to a display-safe string."""
    if v is None:
        return "-"
    if isinstance(v, float):
        if abs(v) >= 100:
            return f"{v:,.1f}"
        return f"{v:.2f}"
    return str(v)


# ── Daily Intake ────────────────────────────────────────────────────────────

def format_intake_targets(intake_data: dict) -> str:
    """Format daily intake targets as a markdown table.

    Args:
        intake_data: Output of compute_daily_intake().
    """
    profile = intake_data.get("profile", {})
    lines = [
        "---",
        "type: daily-intake",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        "tags: [nutrition, intake, targets]",
        "---",
        "",
        "# Daily Intake Targets",
        "",
        f"- **Age**: {profile.get('age')} | **Gender**: {profile.get('gender')} | **Stage**: {profile.get('stage')}",
        "",
        "| Nutrient | Recommended | Tolerable Upper |",
        "|----------|-------------|-----------------|",
    ]

    for n in intake_data.get("nutrients", []):
        lines.append(
            f"| {n['nutrient']} | {_safe(n['recommended'])} | {_safe(n['tolerable'])} |"
        )

    return "\n".join(lines)


# ── Optimal Menu ────────────────────────────────────────────────────────────

def format_optimal_menu(result: dict) -> str:
    """Format an optimized menu as a markdown note.

    Args:
        result: Output of optimize_menu().
    """
    if "error" in result:
        return f"# Optimize Error\n\n{result['error']}"

    lines = [
        "---",
        "type: optimal-menu",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        "tags: [nutrition, menu, optimization]",
        "---",
        "",
        "# Optimal Daily Menu",
        "",
        f"**Daily cost**: {result['total_daily_cost_eur']:.4f} EUR | ",
        f"**Monthly estimate**: {result['total_monthly_cost_eur']:.2f} EUR",
        f"**Foods**: {result['food_count']}",
        "",
        "## Foods",
        "",
        "| Food | Grams/day |",
        "|------|-----------|",
    ]

    menu = result.get("menu", {})
    for food, grams in sorted(menu.items(), key=lambda x: -x[1]):
        lines.append(f"| {food} | {grams:.1f}g |")

    lines.append("")
    lines.append("## Nutrient Coverage")
    lines.append("")
    lines.append("| Nutrient | Recommended | Tolerable | Actual | Status |")
    lines.append("|----------|-------------|-----------|--------|--------|")

    for n in result.get("nutrients", []):
        status_icon = {"ok": "✅", "deficient": "⚠️", "excess": "🔴"}.get(n["status"], "?")
        lines.append(
            f"| {n['nutrient']} | {_safe(n['recommended'])} | {_safe(n['tolerable'])} "
            f"| {_safe(n['actual'])} | {status_icon} {n['status']} |"
        )

    return "\n".join(lines)


# ── Menu Price ──────────────────────────────────────────────────────────────

def format_menu_price(price_data: dict) -> str:
    """Format menu price breakdown as a markdown note.

    Args:
        price_data: Output of price_menu().
    """
    comp = price_data.get("comparison")
    if comp:
        totals = comp.get("totals", {})
        lines = [
            "---",
            "type: menu-price",
            f"date: {datetime.now().strftime('%Y-%m-%d')}",
            "tags: [nutrition, menu, pricing, comparison]",
            "---",
            "",
            "# Menu Price Breakdown & Comparison",
            "",
            "| Metric | Mercadona | Dia |",
            "|--------|-----------|-----|",
            f"| **Daily Cost** | {totals['Mercadona']['total_price_eur']:.4f} EUR | {totals['Dia']['total_price_eur']:.4f} EUR |",
            f"| **Monthly Estimate** | {totals['Mercadona']['total_monthly_eur']:.2f} EUR | {totals['Dia']['total_monthly_eur']:.2f} EUR |",
            f"| **Matched Foods** | {totals['Mercadona']['matched_count']} | {totals['Dia']['matched_count']} |",
            "",
            "## Detailed Item Comparison",
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
            lines.append("")
            lines.append("## Unmatched Foods")
            if totals["Mercadona"]["unmatched"]:
                lines.append("\n**Mercadona Unmatched:**")
                for f in totals["Mercadona"]["unmatched"]:
                    lines.append(f"- {f}")
            if totals["Dia"]["unmatched"]:
                lines.append("\n**Dia Unmatched:**")
                for f in totals["Dia"]["unmatched"]:
                    lines.append(f"- {f}")
        return "\n".join(lines)

    # Fallback to single provider formatting if no comparison data
    lines = [
        "---",
        "type: menu-price",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        "tags: [nutrition, menu, pricing]",
        "---",
        "",
        "# Menu Price Breakdown",
        "",
        f"**Total daily cost**: {price_data['total_price_eur']:.4f} EUR",
        f"**Monthly estimate**: {price_data['total_monthly_eur']:.2f} EUR",
        f"**Matched**: {price_data['matched_count']} foods",
        "",
        "| Food | Amount | Matched Product | Unit Price (EUR/kg) | Cost (EUR) |",
        "|------|--------|-----------------|---------------------|------------|",
    ]

    for food, info in price_data.get("foods", {}).items():
        lines.append(
            f"| {food} | {info.get('amount_g', 0):.0f}g "
            f"| {info.get('matched_product', '-')} "
            f"| {_safe(info.get('unit_price_eur_kg'))} "
            f"| {_safe(info.get('price_eur'))} |"
        )

    if price_data.get("unmatched"):
        lines.append("")
        lines.append("## Unmatched Foods")
        for f in price_data["unmatched"]:
            lines.append(f"- {f}")

    return "\n".join(lines)


# ── Food Details ────────────────────────────────────────────────────────────

def format_food_nutrients(food_data: dict) -> str:
    """Format a food's nutrient profile as a markdown note.

    Args:
        food_data: Output of get_food_nutrients().
    """
    lines = [
        "---",
        "type: food-nutrient",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        "tags: [nutrition, food]",
        "---",
        "",
        f"# {food_data['name']}",
        "",
        "Nutrients per 100g:",
        "",
        "| Nutrient | Amount |",
        "|----------|--------|",
    ]

    for name, amount in sorted(food_data.get("nutrients", {}).items()):
        lines.append(f"| {name} | {_safe(amount)} |")

    return "\n".join(lines)


# ── Food Search / Comparison ────────────────────────────────────────────────

def format_food_search(query: str, foods: list[dict]) -> str:
    """Format food search results as markdown."""
    lines = [
        f"# Food Search: \"{query}\"",
        "",
        f"Found {len(foods)} results.",
        "",
    ]

    for food in foods:
        lines.append(f"## {food['name']}")
        lines.append("")
        for k, v in food.get("nutrients", {}).items():
            lines.append(f"- **{k}**: {_safe(v)}")
        lines.append("")

    return "\n".join(lines)


def format_food_comparison(food_names: list[str], comparisons: list[dict]) -> str:
    """Format a side-by-side food comparison as a markdown table.

    Args:
        food_names: The food names being compared.
        comparisons: List of {name, nutrients} dicts.
    """
    # Collect all nutrient names across all foods
    all_nutrients: set[str] = set()
    for c in comparisons:
        all_nutrients.update(c.get("nutrients", {}).keys())

    # Sort nutrients in a logical order
    priority = ["Energy", "Protein", "Total lipid (fat)", "Carbohydrate, by difference"]
    ordered = [n for n in priority if n in all_nutrients]
    ordered += sorted(all_nutrients - set(priority))

    lines = [
        "---",
        "type: food-comparison",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        "tags: [nutrition, food, comparison]",
        "---",
        "",
        "# Food Comparison",
        "",
        f"Foods: {', '.join(food_names)}",
        "",
        "| Nutrient | " + " | ".join(food_names[:5]) + " |",
        "|----------|" + "|".join(["------" for _ in food_names[:5]]) + "|",
    ]

    for nutrient in ordered:
        row = f"| {nutrient} |"
        for c in comparisons:
            v = c.get("nutrients", {}).get(nutrient, "-")
            row += f" {_safe(v)} |"
        lines.append(row)

    return "\n".join(lines)


# ── Meal Analysis ───────────────────────────────────────────────────────────

def format_meal_analysis(analysis: dict) -> str:
    """Format meal analysis from Nutritionix + Mercadona as a markdown note.

    Args:
        analysis: Output of MealPriceEstimator.analyze_meal().
    """
    if "error" in analysis:
        return f"# Meal Analysis Error\n\n{analysis['error']}"

    totals = analysis.get("totals", {})
    macro = totals.get("macro_split", {})

    lines = [
        "---",
        "type: meal-analysis",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        "tags: [nutrition, meal, pricing]",
        "---",
        "",
        f'# Meal: {analysis["meal_description"]}',
        "",
        "## Totals",
        "",
        f"- **Calories**: {totals.get('calories', 0):.0f} kcal",
        f"- **Protein**: {totals.get('protein', 0):.1f}g ({macro.get('protein_pct', 0)}%)",
        f"- **Carbs**: {totals.get('carbs', 0):.1f}g ({macro.get('carbs_pct', 0)}%)",
        f"- **Fat**: {totals.get('fat', 0):.1f}g ({macro.get('fat_pct', 0)}%)",
        f"- **Estimated cost**: {totals.get('estimated_price_eur', 0):.4f} EUR",
        "",
        "## Foods",
        "",
        "| Food | Serving | kcal | Protein | Carbs | Fat | Price (EUR) |",
        "|------|---------|------|---------|-------|-----|-------------|",
    ]

    for food in analysis.get("foods", []):
        lines.append(
            f"| {food['name']} | {food['serving']} "
            f"| {food['calories']:.0f} "
            f"| {food['protein_g']:.1f}g "
            f"| {food['carbs_g']:.1f}g "
            f"| {food['fat_g']:.1f}g "
            f"| {_safe(food.get('estimated_price_eur'))} |"
        )

    return "\n".join(lines)


# ── Full Menu with Price ────────────────────────────────────────────────────

def format_menu_full(menu_result: dict, price_result: dict | None = None) -> str:
    """Combine optimal menu + pricing into a single comprehensive note.

    Args:
        menu_result: Output of optimize_menu().
        price_result: Optional output of price_menu().
    """
    lines = [
        "---",
        "type: full-menu",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        "tags: [nutrition, menu, optimization, pricing]",
        "---",
        "",
        "# Full Daily Menu",
        "",
    ]

    if "error" not in menu_result:
        lines.append(f"**Daily cost**: {menu_result['total_daily_cost_eur']:.4f} EUR")
        lines.append(f"**Monthly estimate**: {menu_result['total_monthly_cost_eur']:.2f} EUR")
        lines.append("")
        lines.append("## Foods")
        lines.append("")
        lines.append("| Food | Grams | Matched Product | Unit Price | Cost |")
        lines.append("|------|-------|-----------------|------------|------|")

        menu = menu_result.get("menu", {})
        prices = price_result.get("foods", {}) if price_result else {}
        for food, grams in sorted(menu.items(), key=lambda x: -x[1]):
            p = prices.get(food, {})
            lines.append(
                f"| {food} | {grams:.1f}g "
                f"| {p.get('matched_product', '-')} "
                f"| {_safe(p.get('unit_price_eur_kg'))} "
                f"| {_safe(p.get('price_eur'))} |"
            )

        if price_result:
            lines.append("")
            lines.append(f"**Priced total**: {price_result['total_price_eur']:.4f} EUR")

        lines.append("")
        lines.append("## Nutrient Coverage")
        lines.append("")
        lines.append("| Nutrient | Recommended | Tolerable | Actual | Status |")
        lines.append("|----------|-------------|-----------|--------|--------|")

        for n in menu_result.get("nutrients", []):
            s = {"ok": "✅", "deficient": "⚠️", "excess": "🔴"}.get(n["status"], "?")
            lines.append(
                f"| {n['nutrient']} | {_safe(n['recommended'])} | {_safe(n['tolerable'])} "
                f"| {_safe(n['actual'])} | {s} {n['status']} |"
            )

    return "\n".join(lines)

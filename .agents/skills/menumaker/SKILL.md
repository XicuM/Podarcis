---
name: menumaker
description: Computes science-based, cost-optimized healthy menus using USDA food data and linear programming. Use this to design nutritional protocols or investigate food properties.
---

# Menumaker Skill — Nutritional Reasoning & Menu Design

**Use this skill whenever the user asks about:**
- Nutrition, daily nutrient targets, dietary requirements.
- Menu planning, weekly menus, food optimization.
- Food pricing, grocery costs, budget optimization.
- Evaluating objective food ingredients or comparing them.

## What menuMaker Provides

MenuMaker is a suite of tools that compute **science-based, cost-optimized healthy menus** using:
1. **NIH Dietary Reference Intakes** — nutrient targets (RDA + Tolerable Upper Limits).
2. **USDA FoodData Central** — nutrient profiles for thousands of foods.
3. **Linear programming** — finding the cheapest combination of foods meeting all requirements.
4. **Local Pricing** — current grocery prices (e.g., Mercadona).

## The Wiki / Workspace Boundary Strict Workflow

This project adheres strictly to the **wiki (objective) vs workspace (actionable)** separation of concerns. You must use the tools in the appropriate decoupled repository context.

### Objective Knowledge Base (`wiki/` repository)
*   Must remain anonymous and objective. Do not include user-specific data.
*   **Relevant Tools**:
    *   `search_foods(query)`: Search for foods to document.
    *   `get_food_nutrients(food_name)`: Get the objective macronutrient and micronutrient profiles.
*   **Workflow**: When investigating a food's properties, create an objective profile in the `wiki/` repository (e.g., `wiki/nutrition/chicken_breast.md`).

### Actionable Protocols & Workspace Context (`workspace/` repository)
*   Personalized, step-by-step instructions.
*   **Relevant Tools**:
    *   `get_intake_targets(age, gender, stage)`: Computes daily targets. Always extract the arguments (age, gender) from the profile located in the `workspace/` repository.
    *   `optimize_menu(age, gender, stage)`: Runs linear programming to generate the optimal list of raw commodities.
    *   `price_menu(items)`: Calculates the estimated cost of a specific menu.
*   **Workflow**: When generating a meal plan, invoke the optimizer, translate the raw commodities into practical meals, and write an actionable protocol inside `workspace/protocols/` (e.g., `workspace/protocols/weekly_meal_plan.md`). Cite the `wiki/` when referring to specific foods.

## Nutritional Reasoning & Translation Heuristics

The `optimize_menu` tool acts as a commodity solver, not a recipe generator. It will output raw amounts like "423g of raw potatoes".
As the **Protocol Architect**, you must translate these raw outputs into edible, actionable protocols:
1. **Cooking Considerations**: Rice absorbs water (weight increases), meat loses water (weight decreases).
2. **Practicality**: 400g of dry lentils per day is technically cheap, but unrealistic to consume. Distribute these into varied meals.
3. **Fats and Staples**: The optimizer often includes oils (for fat requirements) and cheap grains (for caloric baselines). Structure the meals around these staples.
4. **Meal Timing & Fasting Calibration**: Always ask or check for Time-Restricted Eating (TRE) or Intermittent Fasting schedules (e.g. 16:8 no morning breakfast) before structuring daily meal options.
5. **Validation**: Ensure that your final actionable protocol approximately matches the total macros prescribed by the optimizer.
6. **Diagnostic Logging**: If tool paths, inputs, or user corrections reveal friction during menu design, immediately log the issue using `log_pain_point` (`diagnostics-mcp`).

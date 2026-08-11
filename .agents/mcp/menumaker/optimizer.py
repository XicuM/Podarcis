"""Linear programming optimizer for daily menus.

Treats each food as a vector of nutrients (per 100g) with a EUR cost.
Finds the cheapest combination of foods that satisfies all nutrient targets
(within Recommended to Tolerable windows).
"""

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import linprog

logger = logging.getLogger(__name__)

MENUMAKER_DIR = Path(__file__).resolve().parent
DATA_DIR = os.environ.get("MENUMAKER_DATA_DIR", str(MENUMAKER_DIR / "data"))

def _resolve_file_path(primary: str, fallback_candidates: list[Path]) -> str:
    if os.path.exists(primary):
        return primary
    for candidate in fallback_candidates:
        if candidate.exists():
            return str(candidate)
    return primary

DEFAULT_FOOD_DATA_PATH = _resolve_file_path(
    os.path.join(DATA_DIR, "food_data", "food_data.csv"),
    [
        MENUMAKER_DIR / "data" / "food_data" / "food_data.csv",
        MENUMAKER_DIR / "food_data.csv",
    ],
)

DEFAULT_PRICES_PATH = _resolve_file_path(
    os.path.join(DATA_DIR, "mercadona.csv"),
    [
        MENUMAKER_DIR / "data" / "mercadona.csv",
        MENUMAKER_DIR / "mercadona.csv",
    ],
)


def _load_optimization_data(
    food_data_path: str,
    intake_targets: dict,
    prices_path: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load the three inputs for optimization: food nutrients, intake targets, and prices."""
    food_data_path = _resolve_file_path(
        food_data_path,
        [MENUMAKER_DIR / "data" / "food_data" / "food_data.csv", MENUMAKER_DIR / "food_data.csv"],
    )
    prices_path = _resolve_file_path(
        prices_path,
        [MENUMAKER_DIR / "data" / "mercadona.csv", MENUMAKER_DIR / "mercadona.csv"],
    )
    nutrients = pd.read_csv(food_data_path, index_col=0)

    # Convert intake targets dict to DataFrame
    daily_intake = pd.DataFrame(intake_targets["nutrients"])
    daily_intake = daily_intake.set_index("nutrient")

    # Load price data and clean
    historical_prices = pd.read_csv(prices_path, index_col=0)
    historical_prices = historical_prices.loc[
        historical_prices.iloc[:, -1].dropna().index
    ]
    # Drop known problematic Mercadona IDs
    historical_prices = historical_prices.drop(
        [69297, 34149], axis="index", errors="ignore"
    )
    return nutrients, daily_intake, historical_prices


def build_constraints(
    nutrients: pd.DataFrame,
    daily_intake: pd.DataFrame,
    historical_prices: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, pd.DataFrame, pd.DataFrame]:
    """Build the linear programming constraint matrices.

    Returns:
        prices: Cost vector (shape: n_foods).
        A_ub: Upper-bound constraint matrix (2*n_nutrients x n_foods).
        b_ub: Upper-bound constraint vector (2*n_nutrients).
        nutrient_info: Nutrients for each priced food (n_foods x n_nutrients).
        daily_intake: Prepared intake targets (index = nutrient names).
    """
    prices = historical_prices.iloc[:, -1].to_numpy()

    # Filter to only products whose Info value matches a USDA food name
    info_series = historical_prices["Info"].dropna()
    valid_mask = info_series.isin(nutrients.index)
    if not valid_mask.any():
        raise ValueError(
            "No Mercadona product Info values match USDA food names. "
            "The mercadona.csv Info column needs manual mapping to USDA food names "
            "in food_data.csv."
        )
    valid_ids = info_series[valid_mask].index
    logger.info(
        "Found %d/%d priced foods with matching USDA entries",
        len(valid_ids), len(info_series),
    )

    # Foods matching the Mercadona products
    nutrient_info = nutrients.loc[info_series[valid_mask]]
    prices = historical_prices.loc[valid_ids].iloc[:, -1].to_numpy()

    # Fill defaults
    daily_intake = daily_intake.copy()
    daily_intake["recommended"] = daily_intake["recommended"].fillna(0)
    daily_intake["tolerable"] = daily_intake["tolerable"].fillna(1e6)
    
    # Ensure tolerable is at least recommended to prevent mathematical infeasibility
    # (e.g. Magnesium RDA is 400mg but supplementary UL is 350mg)
    for idx in daily_intake.index:
        rec = daily_intake.loc[idx, "recommended"]
        tol = daily_intake.loc[idx, "tolerable"]
        if tol < rec:
            daily_intake.loc[idx, "tolerable"] = max(rec, 1e6)

    n_nutrients = len(nutrient_info.columns)
    n_foods = len(nutrient_info)

    # Each nutrient contributes two constraints: sum <= Tolerable, sum >= Recommended
    A_ub = np.zeros((2 * n_nutrients, n_foods))
    b_ub = np.zeros(2 * n_nutrients)

    for i, nutrient in enumerate(nutrient_info.columns):
        if nutrient not in daily_intake.index:
            continue
        food_values = np.nan_to_num(nutrient_info[nutrient].to_numpy(), nan=0.0)
        A_ub[2 * i, :] = food_values
        A_ub[2 * i + 1, :] = -food_values
        b_ub[2 * i] = daily_intake.loc[nutrient, "tolerable"]
        b_ub[2 * i + 1] = -daily_intake.loc[nutrient, "recommended"]

    return prices, A_ub, b_ub, nutrient_info, daily_intake


def solve_menu(prices: np.ndarray, A_ub: np.ndarray, b_ub: np.ndarray) -> np.ndarray:
    """Solve the linear program: minimize cost subject to nutrient constraints.

    Returns:
        x: Solution vector (units = 100g portions of each food).
    """
    result = linprog(c=prices, A_ub=A_ub, b_ub=b_ub)
    if result.status != 0:
        raise RuntimeError(f"Optimization failed: {result.message}")
    return result.x  # type: ignore[no-any-return]


def format_solution(
    x: np.ndarray,
    prices: np.ndarray,
    nutrient_info: pd.DataFrame,
    daily_intake: pd.DataFrame,
    A_ub: np.ndarray,
) -> dict[str, Any]:
    """Format the optimization solution into a structured result dictionary."""
    total_cost = round(float(prices @ x), 4)
    menu: dict[str, float] = {}
    for i in range(len(x)):
        grams = round(x[i] * 100, 2)
        if grams > 0:
            food_name = nutrient_info.index[i]
            menu[food_name] = round(menu.get(food_name, 0.0) + grams, 2)

    # Nutrient analysis — only for nutrients that have targets set
    intake = daily_intake.copy()
    result_vector = (A_ub @ x)[::2]  # Every other row is the positive bound

    nutrients_analysis = []
    for i, nutrient_name in enumerate(nutrient_info.columns):
        if nutrient_name not in daily_intake.index:
            continue
        actual = float(result_vector[i])
        rec = daily_intake.loc[nutrient_name, "recommended"]
        tol = daily_intake.loc[nutrient_name, "tolerable"]
        status = (
            "deficient" if actual < rec
            else "excess" if actual > tol
            else "ok"
        )
        nutrients_analysis.append({
            "nutrient": nutrient_name,
            "recommended": float(rec) if not (isinstance(rec, float) and rec != rec) else None,
            "tolerable": float(tol) if not (isinstance(tol, float) and tol != tol) else None,
            "actual": round(actual, 2),
            "status": status,
        })

    return {
        "menu": menu,
        "total_daily_cost_eur": total_cost,
        "total_monthly_cost_eur": round(total_cost * 30, 2),
        "food_count": len(menu),
        "nutrients": nutrients_analysis,
    }


def optimize_menu(
    food_data_path: str | None = None,
    intake_targets: dict | None = None,
    prices_path: str | None = None,
) -> dict[str, Any]:
    """Run the full menu optimization pipeline.

    Args:
        food_data_path: Path to the food nutrient CSV (USDA data).
        intake_targets: Output of compute_daily_intake(). If None, returns error.
        prices_path: Path to the Mercadona price history CSV.

    Returns:
        Dict with 'menu', 'total_daily_cost_eur', 'total_monthly_cost_eur',
        'food_count', and 'nutrients' analysis.
    """
    if intake_targets is None:
        return {"error": "No intake targets provided. Run compute_daily_intake first."}

    if food_data_path is None:
        food_data_path = DEFAULT_FOOD_DATA_PATH
    if prices_path is None:
        prices_path = DEFAULT_PRICES_PATH

    nutrients, daily_intake, historical_prices = _load_optimization_data(
        food_data_path, intake_targets, prices_path
    )
    prices, A_ub, b_ub, nutrient_info, daily_intake = build_constraints(
        nutrients, daily_intake, historical_prices
    )
    x = solve_menu(prices, A_ub, b_ub)
    return format_solution(x, prices, nutrient_info, daily_intake, A_ub)

"""USDA FoodData Central database loader and query interface."""

import os
from pathlib import Path

import numpy as np
import pandas as pd


DATA_DIR = os.environ.get("MENUMAKER_DATA_DIR", str(Path(__file__).parent.parent.parent / "data"))
FOOD_DATA_PATH = os.path.join(DATA_DIR, "food_data", "food_data.csv")

_cached_food_db: pd.DataFrame | None = None


def load_food_db(path: str | None = None) -> pd.DataFrame:
    """Load the USDA food nutrient database from CSV.

    Returns a DataFrame with foods as rows (index) and nutrients as columns.
    Values are per 100g of food.
    """
    global _cached_food_db
    if _cached_food_db is not None:
        return _cached_food_db

    filepath = path or FOOD_DATA_PATH
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Food database not found at {filepath}")

    df = pd.read_csv(filepath, index_col=0)
    _cached_food_db = df
    return df


def build_food_database(usda_json_path: str, output_csv: str | None = None) -> pd.DataFrame:
    """Convert USDA FoodData Central JSON into a nutrient-per-100g CSV.

    Supports both SurveyFoods and FoundationFoods JSON formats.
    If output_csv is given, writes the result to that path.
    """
    with open(usda_json_path, encoding="utf-8") as f:
        data = pd.read_json(f)

    def _get_amount(nutrient: dict):
        try:
            return nutrient["amount"]
        except (KeyError, TypeError):
            return np.nan

    # Support both Survey foods and Foundation foods JSON formats
    if "SurveyFoods" in data:
        food_list = data["SurveyFoods"]
    elif "FoundationFoods" in data:
        food_list = data["FoundationFoods"]
    else:
        raise ValueError("JSON must contain 'SurveyFoods' or 'FoundationFoods' key")

    food_info: dict[str, dict[str, float]] = {}
    for food in food_list:
        nutrients = {
            nutrient["nutrient"]["name"]: _get_amount(nutrient)
            for nutrient in food.get("foodNutrients", [])
        }
        food_info[food["description"]] = nutrients

    food_df = pd.DataFrame(food_info).T
    food_df.index.name = "Food"

    if output_csv:
        food_df.to_csv(output_csv, encoding="utf-8")

    return food_df


def search_foods(query: str, db: pd.DataFrame | None = None) -> pd.DataFrame:
    """Search the food database by name (case-insensitive substring match)."""
    if db is None:
        db = load_food_db()
    return db[db.index.str.contains(query, case=False, na=False)]


def get_food_nutrients(food_name: str, db: pd.DataFrame | None = None) -> dict:
    """Get full nutrient profile for a specific food."""
    if db is None:
        db = load_food_db()
    if food_name not in db.index:
        raise KeyError(f"Food '{food_name}' not found in database")
    row = db.loc[food_name]
    nutrients = row.dropna().to_dict()
    return {
        "name": food_name,
        "nutrients": {k: v for k, v in nutrients.items() if not (isinstance(v, float) and v != v)},
    }


def list_foods(db: pd.DataFrame | None = None) -> list[str]:
    """Return all food names in the database."""
    if db is None:
        db = load_food_db()
    return list(db.index)


_MACRO_NUTRIENTS = [
    "Energy",
    "Protein",
    "Carbohydrate, by difference",
    "Total lipid (fat)",
    "Fiber, total dietary",
    "Sugars, total including NLEA",
]


def get_macros(food_name: str, db: pd.DataFrame | None = None) -> dict:
    """Get just the key macro nutrients for a food, per 100g."""
    if db is None:
        db = load_food_db()
    if food_name not in db.index:
        raise KeyError(f"Food '{food_name}' not found in database")
    row = db.loc[food_name]
    available = [c for c in _MACRO_NUTRIENTS if c in row.index and not pd.isna(row[c])]
    return {"name": food_name, "nutrients": row[available].to_dict()}

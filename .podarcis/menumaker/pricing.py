"""Menu price calculation via fuzzy matching across Mercadona and Dia price providers."""

import logging
import os
from typing import Any
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
from rapidfuzz import process

logger = logging.getLogger(__name__)


def _load_mercadona_mapping(prices_path: str) -> dict[str, int]:
    """Return {food_name: mercadona_id} from mercadona.csv Info column."""
    df = pd.read_csv(prices_path, index_col=0)
    df = df.dropna(subset=["Info"])
    return {row["Info"]: idx for idx, row in df.iterrows()}


def _load_spanish_translation_mapping(prices_path: str) -> dict[str, str]:
    """Return {usda_food_name: spanish_product_name} from mercadona.csv."""
    df = pd.read_csv(prices_path, index_col=0)
    df = df.dropna(subset=["Info", "Product"])
    return {row["Info"]: row["Product"] for idx, row in df.iterrows()}


def _flatten_menu(menu: dict) -> list[tuple[str, float]]:
    """Flatten a nested menu dict into (food_name, amount_g) pairs."""
    items: list[tuple[str, float]] = []
    for key, value in menu.items():
        if isinstance(value, dict):
            items.extend(_flatten_menu(value))
        elif isinstance(value, (int, float)) and value is not None:
            items.append((key, float(value)))
    return items


class BasePriceProvider:
    def name(self) -> str:
        raise NotImplementedError

    def get_price_for_food(self, food_name: str) -> dict[str, Any]:
        """Search and price a specific food item.

        Returns:
            Dict containing:
              - matched_product: name of the matched product
              - unit_price_eur_kg: price per kg/litre/unit
              - provider: provider name
              - error (optional): string detailing any error
        """
        raise NotImplementedError


class MercadonaPriceProvider(BasePriceProvider):
    def __init__(self, prices_path: str):
        self.prices_path = prices_path
        self._mapping = _load_mercadona_mapping(prices_path)

    def name(self) -> str:
        return "Mercadona"

    def get_price_for_food(self, food_name: str) -> dict[str, Any]:
        try:
            closest_match = process.extractOne(food_name, list(self._mapping.keys()))[0]
        except (TypeError, IndexError):
            return {"error": f"No Mercadona match for '{food_name}'", "provider": self.name()}

        product_id = self._mapping[closest_match]

        # 1. Try live pricing via mercapy if installed
        try:
            from mercapy import Product
            product = Product(id=product_id)
            unit_price = product.unit_price  # EUR/kg
            if unit_price is not None:
                return {
                    "matched_product": closest_match,
                    "unit_price_eur_kg": float(unit_price),
                    "provider": self.name(),
                    "product_id": product_id,
                    "source": "live",
                }
        except Exception as e:
            logger.debug("mercapy error for %s: %s. Falling back to historical.", closest_match, e)

        # 2. Fallback to historical prices from CSV
        try:
            df = pd.read_csv(self.prices_path, index_col=0)
            if product_id in df.index:
                row = df.loc[product_id]
            elif int(product_id) in df.index:
                row = df.loc[int(product_id)]
            elif str(product_id) in df.index:
                row = df.loc[str(product_id)]
            else:
                return {"error": f"Product ID {product_id} not found in CSV index", "provider": self.name()}

            price_cols = [c for c in df.columns if c not in ["Product", "Format", "Info"]]
            prices = row[price_cols].dropna()
            if not prices.empty:
                # mercadona.csv logs price as EUR/100g. Convert to EUR/kg.
                price_per_100g = float(prices.iloc[-1])
                unit_price = price_per_100g * 10.0
                return {
                    "matched_product": closest_match,
                    "unit_price_eur_kg": unit_price,
                    "provider": self.name(),
                    "product_id": product_id,
                    "source": "historical",
                }
            else:
                return {"error": f"No historical price columns for '{closest_match}'", "provider": self.name()}
        except Exception as e:
            return {"error": f"Failed to retrieve price: {e}", "provider": self.name()}


class DiaPriceProvider(BasePriceProvider):
    def __init__(self, translation_mapping: dict[str, str]):
        self.translation_mapping = translation_mapping

    def name(self) -> str:
        return "Dia"

    def get_price_for_food(self, food_name: str) -> dict[str, Any]:
        try:
            closest_usda = process.extractOne(food_name, list(self.translation_mapping.keys()))[0]
            spanish_query = self.translation_mapping[closest_usda]
        except Exception as e:
            return {"error": f"No Spanish mapping found for '{food_name}': {e}", "provider": self.name()}

        url = "https://www.dia.es/api/v1/search-back/search"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            r = requests.get(url, params={"q": spanish_query}, headers=headers, timeout=10)
            if r.status_code != 200:
                return {"error": f"Dia API returned status {r.status_code}", "provider": self.name()}

            data = r.json()
            items = data.get("search_items", [])
            if not items:
                # Try a broader search with the first word of the query
                broad_query = spanish_query.split()[0]
                r = requests.get(url, params={"q": broad_query}, headers=headers, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("search_items", [])

            if not items:
                return {"error": f"No Dia products found for '{spanish_query}'", "provider": self.name()}

            display_names = [item["display_name"] for item in items]
            best_match_name = process.extractOne(spanish_query, display_names)[0]
            matched_item = next(item for item in items if item["display_name"] == best_match_name)

            prices = matched_item.get("prices", {})
            unit_price = prices.get("price_per_unit")
            if unit_price is None:
                unit_price = prices.get("price")

            return {
                "matched_product": best_match_name,
                "unit_price_eur_kg": float(unit_price) if unit_price is not None else 0.0,
                "provider": self.name(),
                "sku_id": matched_item.get("sku_id"),
                "brand": matched_item.get("brand"),
                "measure_unit": prices.get("measure_unit"),
            }
        except Exception as e:
            return {"error": f"Dia API search failed: {e}", "provider": self.name()}


def price_menu(
    menu: dict[str, float],
    prices_path: str = "data/mercadona.csv",
) -> dict[str, Any]:
    """Calculate and compare the total price of a menu across Mercadona and Dia providers in parallel.

    Args:
        menu: {food_name: grams} dict (or nested meal→food→grams).
        prices_path: Path to mercadona.csv.

    Returns:
        Dict with Mercadona fallback pricing + comparative metrics.
    """
    mercadona_provider = MercadonaPriceProvider(prices_path)
    translation_mapping = _load_spanish_translation_mapping(prices_path)
    dia_provider = DiaPriceProvider(translation_mapping)

    items = _flatten_menu(menu)
    food_names = [food_name for food_name, amount_g in items if amount_g]

    providers = [mercadona_provider, dia_provider]
    all_prices = {}

    # Query all products in parallel using a thread pool
    if food_names:
        with ThreadPoolExecutor(max_workers=max(1, len(food_names) * len(providers))) as executor:
            futures = {}
            for food_name in food_names:
                futures[food_name] = {}
                for prov in providers:
                    futures[food_name][prov.name()] = executor.submit(prov.get_price_for_food, food_name)

            for food_name in food_names:
                all_prices[food_name] = {}
                for prov in providers:
                    prov_name = prov.name()
                    try:
                        all_prices[food_name][prov_name] = futures[food_name][prov_name].result()
                    except Exception as e:
                        all_prices[food_name][prov_name] = {"error": str(e), "provider": prov_name}

    results: dict[str, dict] = {}
    total_prices = {"Mercadona": 0.0, "Dia": 0.0}
    matched_counts = {"Mercadona": 0, "Dia": 0}
    unmatched = {"Mercadona": [], "Dia": []}

    for food_name, amount_g in items:
        if not amount_g:
            continue

        results[food_name] = {
            "amount_g": amount_g,
            "providers": {}
        }

        prov_results = all_prices.get(food_name, {})
        for prov_name in ["Mercadona", "Dia"]:
            info = prov_results.get(prov_name, {"error": "No price returned", "provider": prov_name})

            if "error" not in info:
                unit_price = info.get("unit_price_eur_kg", 0) or 0
                food_price = unit_price * (amount_g / 1000)
                results[food_name]["providers"][prov_name] = {
                    "matched_product": info["matched_product"],
                    "unit_price_eur_kg": unit_price,
                    "price_eur": round(food_price, 4),
                }
                total_prices[prov_name] += food_price
                matched_counts[prov_name] += 1
            else:
                unmatched[prov_name].append(food_name)
                results[food_name]["providers"][prov_name] = {
                    "error": info["error"],
                    "unit_price_eur_kg": 0.0,
                    "price_eur": 0.0,
                }

    # Maintain backward compatibility with the original return schema using Mercadona as primary
    return {
        "total_price_eur": round(total_prices["Mercadona"], 4),
        "total_monthly_eur": round(total_prices["Mercadona"] * 30, 2),
        "foods": {
            food: {
                "matched_product": info["providers"]["Mercadona"].get("matched_product", "-"),
                "amount_g": info["amount_g"],
                "price_eur": info["providers"]["Mercadona"]["price_eur"],
                "unit_price_eur_kg": info["providers"]["Mercadona"]["unit_price_eur_kg"],
            }
            for food, info in results.items()
        },
        "matched_count": matched_counts["Mercadona"],
        "unmatched": unmatched["Mercadona"],

        # Dynamic comparison object
        "comparison": {
            "foods": results,
            "totals": {
                prov_name: {
                    "total_price_eur": round(total_prices[prov_name], 4),
                    "total_monthly_eur": round(total_prices[prov_name] * 30, 2),
                    "matched_count": matched_counts[prov_name],
                    "unmatched": unmatched[prov_name],
                }
                for prov_name in ["Mercadona", "Dia"]
            }
        }
    }

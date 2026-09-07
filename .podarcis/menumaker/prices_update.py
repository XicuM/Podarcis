"""Mercadona product price scraper and updater."""

import logging
import os
from datetime import datetime as dt

import pandas as pd

logger = logging.getLogger(__name__)

# Required for Mercadona scraping
os.environ.setdefault("PYPPETEER_CHROMIUM_REVISION", "1263111")


def update_prices_via_scraping(prices_path: str = "data/mercadona.csv") -> dict:
    """Scrape current prices from the Mercadona website.

    Uses requests_html with browser rendering for each product.
    Appends a new date column to the CSV.

    Args:
        prices_path: Path to mercadona.csv (read and written).

    Returns:
        Status dict with counts.
    """
    from requests_html import HTMLSession
    from requests.exceptions import RequestException

    BASE_URL = "https://tienda.mercadona.es/product/"
    COOKIE_JAR = [
        {
            "name": "__mo_da",
            "value": '{"warehouse":"bcn1", "postalCode":"08903"}',
            "domain": ".mercadona.es",
        },
        {
            "name": "__mo_ca",
            "value": '{"thirdParty":true,"necessary":true,"version":1}',
            "domain": ".mercadona.es",
        },
    ]

    session = HTMLSession()
    for cookies in COOKIE_JAR:
        session.cookies.update(cookies)

    date = dt.now().strftime("%Y-%m-%d")
    prices = pd.read_csv(prices_path, index_col=0)

    updated = 0
    failed = 0

    for id_ in prices.index:
        try:
            res = session.get(BASE_URL + str(id_), timeout=30)
            res.html.render(sleep=2, timeout=20)
            if info := res.html.find(".product-format__size", first=True):
                price = round(
                    float(
                        info.text.split(" | ")[1].split(" ")[0].replace(",", ".")
                    )
                    / 10,
                    5,
                )
                prices.loc[id_, date] = price
                prices.to_csv(prices_path)
                updated += 1
                logger.info("Updated: %s @ %s EUR/100g", prices.loc[id_, "Product"], price)
        except RequestException:
            logger.error("ERROR: %s", prices.loc[id_, "Product"])
            failed += 1

    session.close()
    return {"status": "ok", "updated": updated, "failed": failed, "date": date}


def update_prices_via_mercapy(prices_path: str = "data/mercadona.csv") -> dict:
    """Update prices using the mercapy library.

    Args:
        prices_path: Path to mercadona.csv (read, prices fetched live).

    Returns:
        Status dict with updated count.
    """
    try:
        from mercapy import Product
    except ImportError:
        return {"status": "error", "message": "mercapy not installed"}

    date = dt.now().strftime("%Y-%m-%d")
    prices = pd.read_csv(prices_path, index_col=0)

    updated = 0
    errors = 0

    for product_id in prices.index:
        try:
            product = Product(id=product_id, warehouse="bcn1")
            if product.unit_price is None:
                logger.error("No price available for %s", prices.loc[product_id, "Product"])
                errors += 1
                continue
            if product.weight:
                price = round(product.unit_price / product.weight / 10, 5)
            else:
                price = round(product.unit_price / 10, 5)
            prices.loc[product_id, date] = price
            updated += 1
        except Exception as e:
            logger.error("Error updating %s: %s", prices.loc[product_id, "Product"] if product_id in prices.index else product_id, e)
            errors += 1

    prices.to_csv(prices_path)
    return {"status": "ok", "updated": updated, "errors": errors, "date": date}

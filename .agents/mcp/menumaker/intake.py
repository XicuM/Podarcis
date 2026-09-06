"""Fetch NIH Dietary Reference Intakes and compute daily intake targets."""

import logging
import os
import re
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

RDA_VIT_URL = "https://www.ncbi.nlm.nih.gov/books/NBK222881/table/ttt00059/?report=objectonly"
RDA_EL_URL = "https://www.ncbi.nlm.nih.gov/books/NBK222881/table/ttt00057_1/?report=objectonly"
UL_VIT_URL = "https://www.ncbi.nlm.nih.gov/books/NBK222881/table/ttt00056/?report=objectonly"
UL_EL_URL = "https://www.ncbi.nlm.nih.gov/books/NBK222881/table/ttt00055_1/?report=objectonly"

RDA_URL = RDA_VIT_URL
UL_URL = UL_VIT_URL

DATA_DIR = os.environ.get("MENUMAKER_DATA_DIR", str(Path(__file__).parent.parent.parent / "data"))
RDA_CACHE_PATH = os.path.join(DATA_DIR, "cache_rda.csv")
UL_CACHE_PATH = os.path.join(DATA_DIR, "cache_ul.csv")

NUTRIENT_MAP = {
    # Vitamins RDA (ttt00059)
    'Choline  e   (mg/d)': 'Choline, total',
    'Choline e  (mg/d)': 'Choline, total',
    'Vitamin C (mg/d)': 'Vitamin C, total ascorbic acid',
    'Vitamin E  f   (mg/d)': 'Vitamin E (alpha-tocopherol)',
    'Vitamin E f  (mg/d)': 'Vitamin E (alpha-tocopherol)',
    'Selenium (μg/d)': 'Selenium, Se',
    'Selenium (µg/d)': 'Selenium, Se',
    
    # Elements & Vit D RDA (ttt00057_1)
    'Calcium (mg/d)': 'Calcium, Ca',
    'Phosphorus (mg/d)': 'Phosphorus, P',
    'Magnesium (mg/d)': 'Magnesium, Mg',
    'Vitamin D (μg/d) a , b': 'Vitamin D (D2 + D3)',
    'Vitamin D (μg/d)  a    ,    b': 'Vitamin D (D2 + D3)',
    'Thiamin (mg/d)': 'Thiamin',
    'Riboflavin (mg/d)': 'Riboflavin',
    'Niacin (mg/d) c': 'Niacin',
    'Niacin (mg/d)  c': 'Niacin',
    'Vitamin B6 (mg/d)': 'Vitamin B-6',
    'Vitamin B6 (mg/d) ': 'Vitamin B-6',
    'Folate (μg/d) d': 'Folate, total',
    'Folate (μg/d)  d': 'Folate, total',
    'Vitamin B12 (μg/d)': 'Vitamin B-12',
    'Vitamin B12 (µg/d)': 'Vitamin B-12',
    'Pantothenic Acid (mg/d)': 'Pantothenic acid',
    'Pantothenic acid (mg/d)': 'Pantothenic acid',
    
    # Vitamins UL (ttt00056)
    'Choline (g/d)': 'Choline, total',
    'Folate (μg/d)  c': 'Folate, total',
    'Folate (μg/d) c': 'Folate, total',
    'Vitamin E (mg/d)  d': 'Vitamin E (alpha-tocopherol)',
    'Vitamin E (mg/d) d': 'Vitamin E (alpha-tocopherol)',
    
    # Elements UL (ttt00055_1)
    'Calcium (g/d)': 'Calcium, Ca',
    'Phosphorus (g/d)': 'Phosphorus, P',
    'Magnesium (mg/d)  b': 'Magnesium, Mg',
    'Magnesium (mg/d) b': 'Magnesium, Mg',
    'Vitamin D (μg/d)': 'Vitamin D (D2 + D3)',
}

STAGE_MAP = {
    "child":      ["Children", "Infants"],
    "adult":      ["Males", "Females"],
    "pregnancy":  ["Pregnancy"],
    "lactation":  ["Lactation"],
}


def _fetch_table(url: str, debug_name: str | None = None) -> pd.DataFrame:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # NCBI renders row-header cells as <th> inside <tbody>, which pandas
    # read_html silently discards. Normalise them to <td>.
    for th in soup.find_all("th"):
        th.name = "td"

    table = pd.read_html(StringIO(str(soup)))[0]
    if debug_name:
        os.makedirs(DATA_DIR, exist_ok=True)
        debug_path = os.path.join(DATA_DIR, f"debug_{debug_name}_table.csv")
        table.to_csv(debug_path, index=False, encoding="utf-8")
        logger.info("Saved %s for inspection.", debug_path)
    return table


def _get_user_stage_gender_age(age: int, gender: str, stage: str) -> tuple[str, str, str]:
    """Map user profile to DRI table keys."""
    gender = gender.capitalize()
    stage = stage.lower()

    if stage == "child":
        if age < 1:
            age_group = "0\u20136 mo"
        elif age < 4:
            age_group = "1\u20133 y"
        elif age < 9:
            age_group = "4\u20138 y"
        elif age < 14:
            age_group = "9\u201313 y"
        elif age < 19:
            age_group = "14\u201318 y"
        else:
            age_group = "19\u201330 y"
    else:
        if age < 19:
            age_group = "14\u201318 y"
        elif age < 31:
            age_group = "19\u201330 y"
        elif age < 51:
            age_group = "31\u201350 y"
        elif age < 71:
            age_group = "51\u201370 y"
        else:
            age_group = "\u226571 y"

    return stage, gender, age_group


def age_group_to_numeric(age_group: str) -> int:
    """Extract first number from age_group like '19-30 y'."""
    nums = [int(s) for s in re.findall(r'\d+', age_group)]
    return nums[0] if nums else 0


def _match_age_row(user_age: int, lsg_series: pd.Series, age_group: str) -> pd.Series:
    """Match the user age to the best row in the life stage group series."""
    norm = lambda s: str(s).replace("–", "-").replace("—", "-").strip().lower()
    age_group_norm = norm(age_group)
    
    # Try exact match first
    for idx, val in lsg_series.items():
        if norm(val) == age_group_norm:
            return lsg_series.index == idx
            
    # Try number range fallback
    matches = []
    for idx, val in lsg_series.items():
        val_str = norm(val)
        if "mo" in val_str:
            if "mo" in age_group_norm:
                tok = age_group_norm.split()[0]
                if tok in val_str:
                    matches.append(idx)
            continue
            
        numbers = [int(s) for s in re.findall(r'\d+', val_str)]
        if not numbers:
            continue
        if ">" in val_str or "≥" in val_str or "older" in val_str:
            if user_age >= numbers[0]:
                matches.append(idx)
        elif "<" in val_str or "≤" in val_str:
            if user_age <= numbers[0]:
                matches.append(idx)
        elif len(numbers) == 2:
            if numbers[0] <= user_age <= numbers[1]:
                matches.append(idx)
        elif len(numbers) == 1:
            if user_age == numbers[0]:
                matches.append(idx)
                
    if matches:
        return lsg_series.index == matches[0]
        
    return pd.Series([True] + [False] * (len(lsg_series) - 1), index=lsg_series.index)


def _map_and_convert(nutrient_raw: str, val_raw: Any) -> tuple[str | None, float | None]:
    """Map raw nutrient name to USDA standard name and apply scale factor if needed."""
    if val_raw is None or (isinstance(val_raw, float) and val_raw != val_raw):
        return None, None
        
    norm = lambda s: str(s).replace("–", "-").replace("—", "-").strip().lower()
    norm_raw = norm(nutrient_raw)
    
    mapped_name = None
    matched_key = None
    for k, v in NUTRIENT_MAP.items():
        if norm(k) == norm_raw:
            mapped_name = v
            matched_key = k
            break
            
    if not mapped_name:
        return None, None
        
    val = _safe_float(val_raw)
    if val is None:
        return mapped_name, None
        
    # Apply conversion factor for grams to milligrams (excluding mg/d, μg/d, µg/d)
    if "g/d" in matched_key and "mg/d" not in matched_key and "μg/d" not in matched_key and "µg/d" not in matched_key:
        val = val * 1000.0
        
    return mapped_name, val


def _fetch_all_tables_live() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch live tables from NCBI and merge Elements with Vitamins."""
    rda_vit = _fetch_table(RDA_VIT_URL, debug_name="rda_vit")
    rda_el = _fetch_table(RDA_EL_URL, debug_name="rda_el")
    ul_vit = _fetch_table(UL_VIT_URL, debug_name="ul_vit")
    ul_el = _fetch_table(UL_EL_URL, debug_name="ul_el")
    
    # 1. Process RDA Elements (rda_el is stacked: block 1 is 0-27, block 2 is 28-50)
    part1 = rda_el.iloc[:28].copy()
    part2 = rda_el.iloc[29:].copy()
    part2_headers = list(rda_el.iloc[28].values)
    part2.columns = part2_headers
    
    # Filter group headers in part1 (where all columns have the same value)
    part1_data = part1[part1.nunique(axis=1) > 1].copy()
    
    # Align part2 with part1_data
    part2_aligned = part2.copy()
    part2_aligned["Life Stage Group"] = part1_data["Life Stage Group"].values
    
    # Merge part1 and part2
    rda_el_merged = part1_data.copy()
    for col in part2_aligned.columns:
        if col != "Life Stage Group":
            rda_el_merged[col] = part2_aligned[col].values
            
    # 2. Process RDA Vitamins (rda_vit)
    # Filter group headers in rda_vit
    rda_vit_data = rda_vit[rda_vit.nunique(axis=1) > 1].copy()
    
    # Merge rda_vit_data with rda_el_merged
    rda_combined = rda_el_merged.copy()
    for col in rda_vit_data.columns:
        if "Life Stage Group" not in str(col):
            rda_combined[col] = rda_vit_data[col].values
            
    # 3. Process UL Tables
    # ul_el has 17 rows, filter group headers to get 12 data rows
    ul_el_lsg = [c for c in ul_el.columns if "Life Stage Group" in str(c)][0]
    ul_el_data = ul_el[ul_el.nunique(axis=1) > 1].copy()
    
    # ul_vit has 12 data rows. Align and merge them.
    ul_vit_aligned = ul_vit.copy()
    ul_vit_aligned["Life Stage Group"] = ul_el_data[ul_el_lsg].values
    
    ul_combined = ul_vit_aligned.copy()
    for col in ul_el_data.columns:
        if "Life Stage Group" not in str(col):
            ul_combined[col] = ul_el_data[col].values
            
    return rda_combined, ul_combined


def get_dri_tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Get DRI tables, fetching live and caching, or loading from cache if offline."""
    try:
        rda_table, ul_table = _fetch_all_tables_live()
        os.makedirs(DATA_DIR, exist_ok=True)
        rda_table.to_csv(RDA_CACHE_PATH, index=False, encoding="utf-8")
        ul_table.to_csv(UL_CACHE_PATH, index=False, encoding="utf-8")
        logger.info("Successfully fetched live DRI tables and cached them.")
        return rda_table, ul_table
    except Exception as e:
        logger.warning("Failed to fetch live DRI tables: %s. Trying local cache...", e)
        if os.path.exists(RDA_CACHE_PATH) and os.path.exists(UL_CACHE_PATH):
            rda_table = pd.read_csv(RDA_CACHE_PATH)
            ul_table = pd.read_csv(UL_CACHE_PATH)
            logger.info("Successfully loaded DRI tables from local cache.")
            return rda_table, ul_table
        else:
            raise RuntimeError(
                f"DRI tables not cached and live fetch failed. "
                f"Please run while online once to populate the cache at {DATA_DIR}."
            ) from e


def _extract_profile_intake(
    table: pd.DataFrame, gender: str, age_group: str, stage: str, value_col_name: str
) -> pd.DataFrame:
    """Extract nutrient intake values from a DRI table for a given profile."""
    # Find the Life Stage Group column
    lsg_col = None
    for c in table.columns:
        if "Life Stage Group" in str(c):
            lsg_col = c
            break

    if not lsg_col:
        lsg_col = table.columns[-1]

    # Clean the table and add group context (forward fill)
    table = table.copy()
    
    current_group = "Infants"
    groups = []
    known_groups = ["infants", "children", "males", "females", "pregnancy", "lactation", "males, females"]
    
    for idx, row in table.iterrows():
        val = str(row[lsg_col]).strip()
        val_lower = val.lower()
        is_group_header = val_lower in known_groups or row.nunique() == 1
        if is_group_header and val_lower in known_groups:
            current_group = val
        groups.append(current_group)
        
    table["_Group"] = groups
    
    # Filter out group header rows (nunique <= 2 since we added _Group)
    table = table[table.nunique(axis=1) > 2]
    
    # Filter by stage/gender group
    group_mask = pd.Series(False, index=table.index)
    stage_lower = stage.lower()
    
    for idx, row in table.iterrows():
        grp = str(row["_Group"]).lower()
        if stage_lower == "pregnancy":
            if "pregnancy" in grp:
                group_mask.loc[idx] = True
        elif stage_lower == "lactation":
            if "lactation" in grp:
                group_mask.loc[idx] = True
        elif stage_lower == "child":
            if "children" in grp or "infants" in grp:
                group_mask.loc[idx] = True
        else: # adult
            if gender.lower() in grp or "males, females" in grp:
                group_mask.loc[idx] = True
                
    group_df = table[group_mask]
    if group_df.empty:
        group_df = table
        
    # Match row by age
    age_row_mask = _match_age_row(age_group_to_numeric(age_group), group_df[lsg_col], age_group)
    match = group_df[age_row_mask]
    
    if match.empty:
        raise ValueError(f"No matching row found for age group '{age_group}'")
        
    row = match.iloc[0]
    nutrients = [c for c in table.columns if c not in [lsg_col, "_Group"]]
    
    df_res = pd.DataFrame({"Nutrient": nutrients, value_col_name: [row[c] for c in nutrients]})
    return df_res


def compute_daily_intake(
    age: int,
    gender: str,
    stage: str = "adult",
) -> dict:
    """Compute daily nutrient intake targets (Recommended and Tolerable) for a user.

    Fetches live from NIH Dietary Reference Intake tables (Vitamins & Elements)
    and maps them to standard USDA database names. Caches results for offline use.

    Args:
        age: Age in years.
        gender: 'male' or 'female'.
        stage: One of 'child', 'adult', 'pregnancy', 'lactation'.

    Returns:
        Dict with keys 'profile' and 'nutrients' ({nutrient, recommended, tolerable}).
    """
    stage_mapped, gender_mapped, age_group = _get_user_stage_gender_age(age, gender, stage)

    # Get DRI tables
    rda_table, ul_table = get_dri_tables()

    # Extract recommendations
    rda_profile = None
    ul_profile = None
    try:
        rda_profile = _extract_profile_intake(rda_table, gender_mapped, age_group, stage_mapped, "Recommended")
    except Exception as e:
        logger.error("Failed to extract RDA profile: %s", e)

    try:
        ul_profile = _extract_profile_intake(ul_table, gender_mapped, age_group, stage_mapped, "Tolerable")
    except Exception as e:
        logger.error("Failed to extract UL profile: %s", e)

    # Merge tables
    if rda_profile is not None and ul_profile is not None:
        merged = pd.merge(rda_profile, ul_profile, on="Nutrient", how="outer")
    elif rda_profile is not None:
        merged = rda_profile
    elif ul_profile is not None:
        merged = ul_profile
    else:
        raise RuntimeError("No matching rows found in either RDA or UL tables.")

    profile_info = {"age": age, "gender": gender, "stage": stage}
    nutrients = []
    
    for _, row in merged.iterrows():
        nutrient_name = row["Nutrient"]
        rec_raw = row.get("Recommended")
        tol_raw = row.get("Tolerable")
        
        mapped_name_rec, rec_val = _map_and_convert(nutrient_name, rec_raw)
        mapped_name_tol, tol_val = _map_and_convert(nutrient_name, tol_raw)
        
        mapped_name = mapped_name_rec or mapped_name_tol
        if not mapped_name:
            continue
            
        nutrients.append({
            "nutrient": mapped_name,
            "recommended": rec_val,
            "tolerable": tol_val,
        })

    # Group nutrients by mapped name to combine entries
    grouped_nutrients = {}
    for n in nutrients:
        name = n["nutrient"]
        if name not in grouped_nutrients:
            grouped_nutrients[name] = {"nutrient": name, "recommended": None, "tolerable": None}
        if n["recommended"] is not None:
            grouped_nutrients[name]["recommended"] = n["recommended"]
        if n["tolerable"] is not None:
            grouped_nutrients[name]["tolerable"] = n["tolerable"]
            
    return {"profile": profile_info, "nutrients": list(grouped_nutrients.values())}


def _safe_float(value) -> float | None:
    """Convert to float, returning None for unparseable values."""
    if value is None:
        return None
    try:
        val_str = str(value).replace("*", "").replace(",", "").strip()
        if val_str.lower() in ["nd", "-", ""]:
            return None
        f = float(val_str)
        if f != f:  # NaN check
            return None
        return f
    except (ValueError, TypeError):
        return None


def load_intake_file(csv_path: str) -> dict:
    """Load daily intake targets from a previously cached CSV file.

    CSV format (same as daily_intake/{name}.csv output):
        Nutrient,Recommended,Tolerable
        Calcium, Ca [mg],1000,2500
        Iron, Fe [mg],8,45
        ...
    """
    df = pd.read_csv(csv_path, index_col=0)
    nutrients = []
    for _, row in df.iterrows():
        nutrients.append({
            "nutrient": row.get("Nutrient", row.name),
            "recommended": _safe_float(row.get("Recommended")),
            "tolerable": _safe_float(row.get("Tolerable")),
        })
    return {"profile": {}, "nutrients": nutrients}


def compute_daily_intake_from_yaml(yaml_path: str) -> dict:
    """Load user profile from a YAML file and compute daily intake.

    YAML format:
        username:
          age: 25
          stage: adult
          gender: male
          weight: 70
          height: 175
          activity_level: moderate
    """
    with open(yaml_path, encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    user_name = list(profile.keys())[0]
    user = profile[user_name]
    return compute_daily_intake(
        age=int(user.get("age", 0)),
        gender=user.get("gender", "male"),
        stage=user.get("stage", "adult"),
    )

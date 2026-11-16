"""
Open Food Facts (https://world.openfoodfacts.org) integration.

Free, no API key, ~3M products worldwide, looked up by barcode. Adds data USDA doesn't have:
Nutri-Score, NOVA processing level, curated allergen/trace tags, additive E-numbers and product photos.

Data is licensed ODbL - the frontend shows an attribution on Open Food Facts products.
Rate limit for product lookups is 100 req/min (per server IP), so results are cached by the caller.
"""
import re

import requests

from .nutrition import build_analysis, empty_nutrient_values, find_allergens

OFF_PRODUCT_URL = 'https://world.openfoodfacts.org/api/v2/product/{barcode}'
# Full-text product search ("search-a-licious"); the old /cgi/search.pl endpoint is unreliable
OFF_SEARCH_URL = 'https://search.openfoodfacts.org/search'
OFF_SEARCH_TIMEOUT = 6  # seconds; text search runs alongside USDA, so keep it snappy
# OFF asks API users to identify their app in the User-Agent
OFF_USER_AGENT = 'ScanMyFood/1.0 (+https://scanmyfood.vercel.app)'
OFF_TIMEOUT = 10  # seconds

OFF_FIELDS = ','.join([
    'code', 'product_name', 'product_name_en', 'generic_name', 'brands', 'categories',
    'ingredients_text', 'ingredients_text_en', 'nutriments', 'nutriscore_grade', 'nova_group',
    'allergens_tags', 'traces_tags', 'additives_tags', 'image_front_url', 'image_url',
    'serving_size', 'quantity', 'countries', 'last_modified_t',
])

# (OFF nutriment key, display name, category, output unit, factor from OFF's grams-per-100g value)
# OFF stores every `<nutrient>_100g` mass value in grams, so we convert to the usual label units.
OFF_NUTRIENTS = [
    ('proteins', 'Protein', 'macronutrients', 'G', 1),
    ('fat', 'Total fat', 'macronutrients', 'G', 1),
    ('carbohydrates', 'Carbohydrates', 'macronutrients', 'G', 1),
    ('fiber', 'Fiber', 'other', 'G', 1),
    ('sugars', 'Total Sugars', 'other', 'G', 1),
    ('saturated-fat', 'Saturated fat', 'other', 'G', 1),
    ('trans-fat', 'Trans fat', 'other', 'G', 1),
    ('cholesterol', 'Cholesterol', 'other', 'MG', 1000),
    ('sodium', 'Sodium', 'minerals', 'MG', 1000),
    ('calcium', 'Calcium', 'minerals', 'MG', 1000),
    ('iron', 'Iron', 'minerals', 'MG', 1000),
    ('magnesium', 'Magnesium', 'minerals', 'MG', 1000),
    ('phosphorus', 'Phosphorus', 'minerals', 'MG', 1000),
    ('potassium', 'Potassium', 'minerals', 'MG', 1000),
    ('zinc', 'Zinc', 'minerals', 'MG', 1000),
    ('vitamin-a', 'Vitamin A', 'vitamins', 'UG', 1_000_000),
    ('vitamin-c', 'Vitamin C', 'vitamins', 'MG', 1000),
    ('vitamin-d', 'Vitamin D', 'vitamins', 'UG', 1_000_000),
    ('vitamin-e', 'Vitamin E', 'vitamins', 'MG', 1000),
    ('vitamin-k', 'Vitamin K', 'vitamins', 'UG', 1_000_000),
    ('vitamin-b1', 'Thiamin', 'vitamins', 'MG', 1000),
    ('vitamin-b2', 'Riboflavin', 'vitamins', 'MG', 1000),
    ('vitamin-pp', 'Niacin', 'vitamins', 'MG', 1000),
    ('vitamin-b6', 'Vitamin B-6', 'vitamins', 'MG', 1000),
    ('vitamin-b9', 'Folate', 'vitamins', 'UG', 1_000_000),
    ('vitamin-b12', 'Vitamin B-12', 'vitamins', 'UG', 1_000_000),
]

# Map OFF allergen tags onto the same names the USDA ingredient matcher uses
OFF_ALLERGEN_NAMES = {
    'milk': 'milk', 'eggs': 'egg', 'peanuts': 'peanut', 'nuts': 'tree nut', 'soybeans': 'soy',
    'gluten': 'gluten', 'fish': 'fish', 'crustaceans': 'shellfish', 'molluscs': 'shellfish',
    'sesame-seeds': 'sesame', 'celery': 'celery', 'mustard': 'mustard', 'lupin': 'lupin',
    'sulphur-dioxide-and-sulphites': 'sulphites',
}

VALID_BARCODE_LENGTHS = (8, 12, 13, 14)


class OpenFoodFactsError(Exception):
    pass


def normalize_barcode(raw):
    """Return the barcode as digits only, or None if it isn't a plausible EAN/UPC."""
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    return digits if len(digits) in VALID_BARCODE_LENGTHS else None


def fetch_product(barcode):
    """
    Look a product up by barcode. Returns the raw OFF product dict, or None if it isn't in the database.
    Raises OpenFoodFactsError on network/upstream errors.
    """
    try:
        response = requests.get(
            OFF_PRODUCT_URL.format(barcode=barcode),
            params={'fields': OFF_FIELDS},
            headers={'User-Agent': OFF_USER_AGENT, 'accept': 'application/json'},
            timeout=OFF_TIMEOUT,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        raise OpenFoodFactsError(str(e)) from e

    if data.get('status') != 1 or not data.get('product'):
        return None
    return data['product']


def _tag_names(tags):
    """'en:sesame-seeds' -> 'sesame-seeds' (drops the language prefix)."""
    return [tag.split(':', 1)[-1].strip().lower() for tag in tags or [] if tag]


def _as_text(value):
    """Search results sometimes return lists (e.g. brands=['Sara Lee']) where the product API has text."""
    if isinstance(value, list):
        return ', '.join(str(v) for v in value if v)
    return str(value or '')


def _first_text(product, *keys):
    for key in keys:
        value = _as_text(product.get(key)).strip()
        if value:
            return value
    return None


def _pick_ingredients(product):
    """
    Prefer the English ingredient list, unless it's a partial community translation
    (e.g. Nutella's English text is just "sugar, palm oil") - then use the original language.
    """
    english = _first_text(product, 'ingredients_text_en')
    original = _first_text(product, 'ingredients_text')
    if english and original and len(english) < 0.6 * len(original):
        return original
    return english or original


def _pick_category(categories):
    """Most specific category, skipping language-tagged entries like "fr:Nutella"."""
    if not categories:
        return None
    parts = [part.strip() for part in categories.split(',') if part.strip()]
    readable = [part for part in parts if ':' not in part]
    return (readable or parts)[-1] if parts else None


def process_off_product(product):
    """Normalize an Open Food Facts product into the shared FoodData shape (values per 100 g)."""
    nutriments = product.get('nutriments') or {}
    ingredients = _pick_ingredients(product)

    calories = nutriments.get('energy-kcal_100g')
    if calories is None and nutriments.get('energy_100g') is not None:
        calories = nutriments['energy_100g'] / 4.184  # energy_100g is in kJ
    try:
        calories = round(float(calories), 1) if calories is not None else None
    except (TypeError, ValueError):
        calories = None

    brands = _first_text(product, 'brands')
    categories = _first_text(product, 'categories')

    basic_info = {
        'name': _first_text(product, 'product_name_en', 'product_name', 'generic_name') or 'Unknown Product',
        'brand': brands.split(',')[0].strip() if brands else None,
        'data_type': 'Open Food Facts',
        'id': None,  # no USDA FDC ID; the barcode is the identifier
        'upc': product.get('code'),
        'category': _pick_category(categories),
        'ingredients': ingredients,
        # OFF nutrition is normalized per 100 g; the package's own serving is shown separately
        'serving_size': 100,
        'serving_unit': 'g',
        'household_serving': None,
        'package_serving': product.get('serving_size'),
        'quantity': product.get('quantity'),
        'calories': calories,
        'published_date': None,
        'market_country': product.get('countries'),
        'source': 'openfoodfacts',
        'image_url': product.get('image_front_url') or product.get('image_url'),
    }

    nutrients = {'macronutrients': [], 'vitamins': [], 'minerals': [], 'other': []}
    nutrient_values = empty_nutrient_values()
    nutrient_values['calories'] = calories
    value_keys = {'proteins': 'protein', 'fat': 'fat', 'carbohydrates': 'carbs', 'fiber': 'fiber',
                  'sugars': 'sugars', 'sodium': 'sodium', 'cholesterol': 'cholesterol',
                  'saturated-fat': 'saturated_fat', 'trans-fat': 'trans_fat'}

    for key, name, category, unit, factor in OFF_NUTRIENTS:
        raw = nutriments.get(f'{key}_100g')
        if raw is None:
            continue
        try:
            amount = round(float(raw) * factor, 2)
        except (TypeError, ValueError):
            continue
        nutrients[category].append({'name': name, 'amount': amount, 'unit': unit, 'daily_value_percent': None})
        if key in value_keys:
            nutrient_values[value_keys[key]] = amount

    # Prefer OFF's curated allergen tags; fall back to ingredient text matching when there are none
    allergens = []
    for tag in _tag_names(product.get('allergens_tags')):
        name = OFF_ALLERGEN_NAMES.get(tag, tag.replace('-', ' '))
        if name not in allergens:
            allergens.append(name)
    if not allergens:
        allergens = find_allergens(ingredients)

    additives = [tag.upper() for tag in _tag_names(product.get('additives_tags'))]

    analysis = build_analysis(nutrient_values, ingredients, allergens=allergens,
                              additives=additives, additives_are_codes=True)

    grade = (product.get('nutriscore_grade') or '').lower()
    analysis['nutri_score'] = grade if grade in ('a', 'b', 'c', 'd', 'e') else None
    nova = product.get('nova_group')
    analysis['nova_group'] = int(nova) if str(nova).isdigit() and 1 <= int(nova) <= 4 else None
    traces = []
    for tag in _tag_names(product.get('traces_tags')):
        name = OFF_ALLERGEN_NAMES.get(tag, tag.replace('-', ' '))
        if name not in traces:
            traces.append(name)
    analysis['traces'] = traces

    if analysis['nutri_score']:
        analysis['key_highlights'].insert(0, f"Nutri-Score {analysis['nutri_score'].upper()}")
    if analysis['nova_group'] == 4:
        analysis['key_highlights'].append("Ultra-processed (NOVA 4)")

    return {'basic_info': basic_info, 'nutrients': nutrients, 'analysis': analysis}


def _query_words(query):
    return [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]


def search_products(query, limit=10):
    """
    Text search Open Food Facts (worldwide products). Its ranking is fuzzy, so only products whose
    name or brand contains every word of the query are kept. Raises OpenFoodFactsError on failure.
    """
    words = _query_words(query)
    if not words:
        return []
    try:
        response = requests.get(
            OFF_SEARCH_URL,
            params={'q': query, 'page_size': 30, 'fields': OFF_FIELDS},
            headers={'User-Agent': OFF_USER_AGENT, 'accept': 'application/json'},
            timeout=OFF_SEARCH_TIMEOUT,
        )
        response.raise_for_status()
        hits = response.json().get('hits', [])
    except (requests.RequestException, ValueError) as e:
        raise OpenFoodFactsError(str(e)) from e

    results = []
    for product in hits:
        if not product.get('code'):
            continue
        text = ' '.join(_as_text(product.get(k)) for k in ('product_name', 'product_name_en', 'brands')).lower()
        if all(word in text for word in words):
            results.append(process_off_product(product))
        if len(results) >= limit:
            break
    return results

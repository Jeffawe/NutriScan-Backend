"""
Pure nutrition-processing helpers (no Redis / network / ML imports), so they can be unit tested.

Every data source (USDA, Open Food Facts) is normalized into the same shape:
    {"basic_info": {...}, "nutrients": {...}, "analysis": {...}}
which is what the frontend's `FoodData` type expects.
"""
import re

# Allergen -> ingredient patterns (word-boundary regexes). This is a keyword heuristic,
# not a substitute for the label: "may contain" / cross-contamination isn't detectable here.
ALLERGEN_PATTERNS = {
    'milk': [r'milk', r'buttermilk', r'whey', r'casein(ate)?', r'lactose', r'cheese', r'yogh?urt',
             r'(?<!peanut )(?<!cocoa )(?<!shea )(?<!nut )butter', r'cream(?! of tartar)', r'ghee', r'dairy'],
    'egg': [r'eggs?', r'egg whites?', r'egg yolks?', r'albumin', r'mayonnaise'],
    'peanut': [r'peanuts?'],
    'tree nut': [r'tree nuts?', r'almonds?', r'cashews?', r'walnuts?', r'pecans?', r'hazelnuts?',
                 r'pistachios?', r'macadamias?', r'brazil nuts?', r'pine nuts?'],
    'soy': [r'soy', r'soya', r'soybeans?', r'tofu', r'edamame'],
    'wheat': [r'wheat', r'semolina', r'durum', r'spelt', r'farina', r'couscous'],
    'gluten': [r'gluten', r'barley', r'rye', r'malt'],
    'fish': [r'fish', r'anchov(y|ies)', r'cod', r'salmon', r'tuna', r'tilapia', r'sardines?'],
    'shellfish': [r'shellfish', r'shrimp', r'prawns?', r'crabs?', r'lobsters?', r'clams?',
                  r'oysters?', r'mussels?', r'scallops?', r'crayfish'],
    'sesame': [r'sesame', r'tahini'],
}
ALLERGEN_REGEXES = {
    allergen: re.compile(r'\b(' + '|'.join(patterns) + r')\b', re.IGNORECASE)
    for allergen, patterns in ALLERGEN_PATTERNS.items()
}

COMMON_ADDITIVES = [
    "aspartame", "sucralose", "saccharin", "high fructose", "msg",
    "monosodium glutamate", "artificial", "preservative", "benzoate",
    "nitrite", "nitrate", "bht", "bha", "red dye", "yellow dye", "blue dye"
]

USDA_NUTRIENT_CATEGORIES = {
    'macronutrients': ['Protein', 'Total lipid (fat)', 'Carbohydrate, by difference'],
    'vitamins': ['Vitamin A', 'Vitamin C', 'Vitamin D', 'Vitamin E', 'Vitamin K',
                 'Thiamin', 'Riboflavin', 'Niacin', 'Vitamin B-6', 'Folate', 'Vitamin B-12'],
    'minerals': ['Calcium', 'Iron', 'Magnesium', 'Phosphorus', 'Potassium', 'Sodium', 'Zinc',
                 'Copper', 'Selenium'],
    'other': ['Fiber, total dietary', 'Total Sugars', 'Cholesterol',
              'Fatty acids, total saturated', 'Fatty acids, total trans']
}


def empty_nutrient_values():
    return {
        'calories': None, 'fat': None, 'sodium': None, 'fiber': None, 'protein': None,
        'carbs': None, 'sugars': None, 'cholesterol': None, 'saturated_fat': None, 'trans_fat': None,
    }


def find_allergens(ingredients):
    if not ingredients:
        return []
    return [allergen for allergen, regex in ALLERGEN_REGEXES.items() if regex.search(ingredients)]


def find_additives(ingredients):
    if not ingredients:
        return []
    ingredients_lower = ingredients.lower()
    return [additive for additive in COMMON_ADDITIVES if additive in ingredients_lower]


def build_analysis(nutrient_values, ingredients, allergens=None, additives=None, additives_are_codes=False):
    """
    Health metrics, macro calorie split and highlight badges, shared by every data source.

    allergens / additives: pass a list to use source-provided data (e.g. Open Food Facts tags);
    leave as None to detect them from the ingredient text.
    """
    nv = nutrient_values

    def has(key):
        return nv.get(key) is not None

    analysis = {
        'health_metrics': {
            'is_low_fat': has('fat') and nv['fat'] <= 3,
            'is_low_sodium': has('sodium') and nv['sodium'] <= 140,
            'is_high_fiber': has('fiber') and nv['fiber'] >= 5,
            'is_low_calorie': has('calories') and nv['calories'] <= 40,
            'is_high_protein': has('protein') and nv['protein'] >= 5,
        },
        'nutritional_profile': {},
        'key_highlights': [],
        'additives': additives if additives is not None else find_additives(ingredients),
        'allergens': allergens if allergens is not None else find_allergens(ingredients),
    }

    profile = analysis['nutritional_profile']
    if has('calories'):
        profile['calories_per_serving'] = nv['calories']
        if nv['calories'] > 0:
            for key, kcal_per_gram, out in (('fat', 9, 'fat_calories_percent'),
                                            ('protein', 4, 'protein_calories_percent'),
                                            ('carbs', 4, 'carbs_calories_percent')):
                if has(key):
                    profile[out] = round(nv[key] * kcal_per_gram / nv['calories'] * 100, 1)

    highlights = []
    if has('calories') and nv['calories'] == 0:
        highlights.append("Zero calories")

    if has('sodium') and nv['sodium'] > 400:
        highlights.append("High sodium content")
    elif has('sodium') and nv['sodium'] < 140:
        highlights.append("Low sodium")

    if has('fiber') and nv['fiber'] >= 5:
        highlights.append("Good source of fiber")

    if has('protein') and nv['protein'] >= 5:
        highlights.append("Good source of protein")

    if has('fat') and nv['fat'] <= 3:
        highlights.append("Low fat")
    elif has('fat') and nv['fat'] >= 15:
        highlights.append("High fat content")

    if analysis['additives']:
        if additives_are_codes:
            count = len(analysis['additives'])
            highlights.append(f"Contains {count} additive{'s' if count != 1 else ''}")
        else:
            highlights.append("Contains artificial additives")

    if analysis['allergens']:
        highlights.append(f"Contains allergens: {', '.join(analysis['allergens'])}")

    analysis['key_highlights'] = highlights
    return analysis


def process_usda_food(food_data):
    """
    Process a USDA FoodData Central record (search result or /food/{id} detail) into the shared shape.
    """
    basic_info = {
        'name': food_data.get('description', 'Unknown Food'),
        'brand': food_data.get('brandOwner', food_data.get('brandName', 'Unknown Brand')),
        'id': food_data.get('fdcId', None),
        'upc': food_data.get('gtinUpc', None),
        'category': food_data.get('foodCategory', food_data.get('brandedFoodCategory', None)),
        'ingredients': food_data.get('ingredients', None),
        'serving_size': food_data.get('servingSize', None),
        'serving_unit': food_data.get('servingSizeUnit', None),
        'household_serving': food_data.get('householdServingFullText', None),
        'calories': None,  # Will be updated if available
        'published_date': food_data.get('publishedDate', food_data.get('publicationDate', None)),
        'market_country': food_data.get('marketCountry', None),
        'available_date': food_data.get('availableDate', None),
        'discontinued_date': food_data.get('discontinuedDate', None),
        'source': 'usda',
        'image_url': None,
    }
    # FoodCategory can be a nested object on the detail endpoint
    if isinstance(basic_info['category'], dict):
        basic_info['category'] = basic_info['category'].get('description')

    nutrients = {category: [] for category in USDA_NUTRIENT_CATEGORIES}
    nutrient_values = empty_nutrient_values()

    # Label nutrients (branded detail format)
    label_nutrients = food_data.get('labelNutrients') or {}
    for label_key, value_key in (('calories', 'calories'), ('fat', 'fat'), ('sodium', 'sodium'),
                                 ('protein', 'protein'), ('carbohydrates', 'carbs')):
        if label_nutrients.get(label_key) is not None:
            nutrient_values[value_key] = label_nutrients[label_key].get('value')
    basic_info['calories'] = nutrient_values['calories']

    got_kcal_from_nutrients = False
    for nutrient in food_data.get('foodNutrients') or []:
        if 'nutrientName' in nutrient:
            # Search-result format
            nutrient_name = nutrient.get('nutrientName', '')
            amount = nutrient.get('value')
            unit = nutrient.get('unitName', '')
            daily_value = nutrient.get('percentDailyValue')
        elif 'nutrient' in nutrient:
            # Detail format with nested nutrient object
            nutrient_name = nutrient['nutrient'].get('name', '')
            amount = nutrient.get('amount')
            unit = nutrient['nutrient'].get('unitName', '')
            daily_value = None
        else:
            continue

        if not nutrient_name or amount is None:
            continue

        # Energy is often listed twice (KCAL and kJ), and Foundation foods use
        # "Energy (Atwater ...)" names; only take kcal, and prefer the plain "Energy" row.
        if nutrient_name.startswith('Energy') and (unit or '').upper() == 'KCAL':
            if nutrient_name == 'Energy' or not got_kcal_from_nutrients:
                basic_info['calories'] = amount
                nutrient_values['calories'] = amount
                got_kcal_from_nutrients = True

        lower = nutrient_name.lower()
        if 'Total lipid (fat)' in nutrient_name:
            nutrient_values['fat'] = amount
        elif 'Sodium' in nutrient_name:
            nutrient_values['sodium'] = amount
        elif 'Fiber, total dietary' in nutrient_name:
            nutrient_values['fiber'] = amount
        elif nutrient_name == 'Protein':
            nutrient_values['protein'] = amount
        elif 'Carbohydrate' in nutrient_name:
            nutrient_values['carbs'] = amount
        elif 'Total Sugars' in nutrient_name:
            nutrient_values['sugars'] = amount
        elif 'Cholesterol' in nutrient_name:
            nutrient_values['cholesterol'] = amount
        elif 'total saturated' in lower:
            nutrient_values['saturated_fat'] = amount
        elif 'total trans' in lower:
            nutrient_values['trans_fat'] = amount

        entry = {'name': nutrient_name, 'amount': amount, 'unit': unit, 'daily_value_percent': daily_value}
        for category, nutrient_list in USDA_NUTRIENT_CATEGORIES.items():
            if any(n in nutrient_name for n in nutrient_list):
                nutrients[category].append(entry)
                break
        else:
            nutrients['other'].append(entry)

    return {
        'basic_info': basic_info,
        'nutrients': nutrients,
        'analysis': build_analysis(nutrient_values, basic_info['ingredients']),
    }

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
import requests
from PIL import Image
import io
from dotenv import load_dotenv
import os
from django.shortcuts import render

# Get the directory of the current script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env from the same directory
load_dotenv(os.path.join(BASE_DIR, '.env'))

def home(request):
    return render(request, 'home.html')

# Create your views here.
class FoodProductView(APIView):
    def get(self, request):
        #Get Product Name from react request
        product_id = request.query_params.get('fcID')

        if not product_id:
            return Response({"error": "Missing 'name' parameter"}, status=400)

        api_key = os.getenv('USDA_API_KEY')

        headers = {
            'accept': 'application/json',
        }

        usda_api_url = f'https://api.nal.usda.gov/fdc/v1/food/{product_id}'
        params = {
            'format': 'full',
            'api_key': api_key
        }

        response = requests.get(usda_api_url, headers=headers, params=params)
        food_data = response.json()

        processed_data = self.process_food_data(food_data=food_data)
        return Response(processed_data)

    @staticmethod
    def process_food_data(food_data):
        """
        Process food data from data.gov API into a structured format.
        Handles multiple data formats, missing data, and extracts comprehensive nutritional information.

        Args:
            food_data (dict): Raw food data from data.gov API

        Returns:
            dict: Processed food information with basic info, nutrients, and analysis
        """
        # Group nutrients by category
        nutrient_categories = {
            'macronutrients': ['Protein', 'Total lipid (fat)', 'Carbohydrate, by difference'],
            'vitamins': ['Vitamin A', 'Vitamin C', 'Vitamin D', 'Vitamin E', 'Vitamin K',
                         'Thiamin', 'Riboflavin', 'Niacin', 'Vitamin B-6', 'Folate', 'Vitamin B-12'],
            'minerals': ['Calcium', 'Iron', 'Magnesium', 'Phosphorus', 'Potassium', 'Sodium', 'Zinc',
                         'Copper', 'Selenium'],
            'other': ['Fiber, total dietary', 'Total Sugars', 'Cholesterol',
                      'Fatty acids, total saturated', 'Fatty acids, total trans']
        }

        # Extract comprehensive basic info
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
        }

        # Initialize nutrients structure
        nutrients = {category: [] for category in nutrient_categories}

        # Track specific nutrients for health metrics
        nutrient_values = {
            'calories': None,
            'fat': None,
            'sodium': None,
            'fiber': None,
            'protein': None,
            'carbs': None,
            'sugars': None,
            'cholesterol': None,
            'saturated_fat': None,
            'trans_fat': None,
        }

        # Get label nutrients if available (new format)
        if 'labelNutrients' in food_data and food_data['labelNutrients']:
            label_nutrients = food_data['labelNutrients']
            if 'calories' in label_nutrients and label_nutrients['calories'] is not None:
                nutrient_values['calories'] = label_nutrients['calories'].get('value')
                basic_info['calories'] = nutrient_values['calories']

            if 'fat' in label_nutrients and label_nutrients['fat'] is not None:
                nutrient_values['fat'] = label_nutrients['fat'].get('value')

            if 'sodium' in label_nutrients and label_nutrients['sodium'] is not None:
                nutrient_values['sodium'] = label_nutrients['sodium'].get('value')

            if 'protein' in label_nutrients and label_nutrients['protein'] is not None:
                nutrient_values['protein'] = label_nutrients['protein'].get('value')

            if 'carbohydrates' in label_nutrients and label_nutrients['carbohydrates'] is not None:
                nutrient_values['carbs'] = label_nutrients['carbohydrates'].get('value')

        # Process food nutrients if available in original format
        if 'foodNutrients' in food_data and food_data['foodNutrients']:
            for nutrient in food_data['foodNutrients']:
                # Handle different nutrient data structures
                if 'nutrientName' in nutrient:
                    # Original format
                    nutrient_name = nutrient.get('nutrientName', '')
                    amount = nutrient.get('value')
                    unit = nutrient.get('unitName', '')
                    daily_value = nutrient.get('percentDailyValue')
                elif 'nutrient' in nutrient:
                    # New format with nested nutrient object
                    nutrient_name = nutrient['nutrient'].get('name', '')
                    amount = nutrient.get('amount')
                    unit = nutrient['nutrient'].get('unitName', '')
                    daily_value = None  # This format might not include percentDailyValue
                else:
                    continue  # Skip if essential structure is missing

                # Skip if essential data is missing
                if not nutrient_name or amount is None:
                    continue

                # Get calories
                if nutrient_name == 'Energy':
                    basic_info['calories'] = amount
                    nutrient_values['calories'] = amount

                # Extract key nutrients for health metrics
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
                elif 'saturated' in nutrient_name.lower():
                    nutrient_values['saturated_fat'] = amount
                elif 'trans' in nutrient_name.lower():
                    nutrient_values['trans_fat'] = amount

                # Categorize nutrients
                categorized = False
                for category, nutrient_list in nutrient_categories.items():
                    if any(n in nutrient_name for n in nutrient_list):
                        nutrients[category].append({
                            'name': nutrient_name,
                            'amount': amount,
                            'unit': unit,
                            'daily_value_percent': daily_value
                        })
                        categorized = True
                        break

                # Add to "other" if not categorized and has a value
                if not categorized and amount is not None:
                    nutrients['other'].append({
                        'name': nutrient_name,
                        'amount': amount,
                        'unit': unit,
                        'daily_value_percent': daily_value
                    })

        # Add analysis and insights
        analysis = {
            'health_metrics': {
                'is_low_fat': nutrient_values['fat'] is not None and nutrient_values['fat'] <= 3,
                'is_low_sodium': nutrient_values['sodium'] is not None and nutrient_values['sodium'] <= 140,
                'is_high_fiber': nutrient_values['fiber'] is not None and nutrient_values['fiber'] >= 5,
                'is_low_calorie': nutrient_values['calories'] is not None and nutrient_values['calories'] <= 40,
                'is_high_protein': nutrient_values['protein'] is not None and nutrient_values['protein'] >= 5,
            },
            'nutritional_profile': {},
            'key_highlights': [],
            'additives': [],
            'allergens': []
        }

        # Check for common allergens in ingredients
        common_allergens = [
            "milk", "dairy", "egg", "peanut", "tree nut", "soy", "wheat",
            "gluten", "fish", "shellfish", "sesame"
        ]

        # Check for common additives
        common_additives = [
            "aspartame", "sucralose", "saccharin", "high fructose", "msg",
            "monosodium glutamate", "artificial", "preservative", "benzoate",
            "nitrite", "nitrate", "bht", "bha", "red dye", "yellow dye", "blue dye"
        ]

        # Process ingredients for additives and allergens if available
        if basic_info['ingredients']:
            ingredients_lower = basic_info['ingredients'].lower()

            # Check for allergens
            found_allergens = []
            for allergen in common_allergens:
                if allergen in ingredients_lower:
                    found_allergens.append(allergen)

            if found_allergens:
                analysis['allergens'] = found_allergens

            # Check for additives
            found_additives = []
            for additive in common_additives:
                if additive in ingredients_lower:
                    found_additives.append(additive)

            if found_additives:
                analysis['additives'] = found_additives

        # Add nutritional profile analysis
        if nutrient_values['calories'] is not None:
            analysis['nutritional_profile']['calories_per_serving'] = nutrient_values['calories']

        if nutrient_values['fat'] is not None and nutrient_values['calories'] is not None and nutrient_values[
            'calories'] > 0:
            fat_cal_percent = (nutrient_values['fat'] * 9 / nutrient_values['calories']) * 100
            analysis['nutritional_profile']['fat_calories_percent'] = round(fat_cal_percent, 1)

        if nutrient_values['protein'] is not None and nutrient_values['calories'] is not None and nutrient_values[
            'calories'] > 0:
            protein_cal_percent = (nutrient_values['protein'] * 4 / nutrient_values['calories']) * 100
            analysis['nutritional_profile']['protein_calories_percent'] = round(protein_cal_percent, 1)

        if nutrient_values['carbs'] is not None and nutrient_values['calories'] is not None and nutrient_values[
            'calories'] > 0:
            carbs_cal_percent = (nutrient_values['carbs'] * 4 / nutrient_values['calories']) * 100
            analysis['nutritional_profile']['carbs_calories_percent'] = round(carbs_cal_percent, 1)

        # Generate key highlights
        highlights = []

        if nutrient_values['calories'] is not None and nutrient_values['calories'] == 0:
            highlights.append("Zero calories")

        if nutrient_values['sodium'] is not None and nutrient_values['sodium'] > 400:
            highlights.append("High sodium content")
        elif nutrient_values['sodium'] is not None and nutrient_values['sodium'] < 140:
            highlights.append("Low sodium")

        if nutrient_values['fiber'] is not None and nutrient_values['fiber'] >= 5:
            highlights.append("Good source of fiber")

        if nutrient_values['protein'] is not None and nutrient_values['protein'] >= 5:
            highlights.append("Good source of protein")

        if nutrient_values['fat'] is not None and nutrient_values['fat'] <= 3:
            highlights.append("Low fat")
        elif nutrient_values['fat'] is not None and nutrient_values['fat'] >= 15:
            highlights.append("High fat content")

        if analysis['additives']:
            highlights.append("Contains artificial additives")

        if analysis['allergens']:
            allergen_list = ", ".join(analysis['allergens'])
            highlights.append(f"Contains allergens: {allergen_list}")

        analysis['key_highlights'] = highlights

        return {
            'basic_info': basic_info,
            'nutrients': nutrients,
            'analysis': analysis
        }

class FoodProductViewMany(APIView):
    def get(self, request):
        #Get Product Name from react request
        product_name = request.query_params.get('name')

        if not product_name:
            return Response({"error": "Missing 'name' parameter"}, status=400)

        api_key = os.getenv('USDA_API_KEY')

        headers = {
            'accept': 'application/json',
        }

        usda_api_url = 'https://api.nal.usda.gov/fdc/v1/foods/search'
        params = {
            'query': product_name,
            'pageSize': 25,
            'pageNumber': 2,
            'dataType': 'Foundation, Branded',
            'sortBy': 'publishedDate',
            'sortOrder': 'asc',
            'api_key': api_key
        }

        response = requests.get(usda_api_url, headers=headers, params=params)
        food_data = response.json()
        result = []

        for data in food_data['foods']:
            processed_data = FoodProductView.process_food_data(food_data=data)
            result.append(processed_data)

        return Response(result)

class FoodImageAnalysisView(APIView):
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        if 'image' not in request.FILES:
            return Response({'error': 'No image provided'}, status=400)

        image_file = request.FILES['image']

        # Convert uploaded file to PIL Image
        image = Image.open(io.BytesIO(image_file.read()))

        # Do image processing here
        processed_results = self.process_image(image)

        return Response(processed_results)

    def process_image(self, image):
        # Example image processing
        # You could:
        # 1. Use OCR to read text
        # 2. Use computer vision to detect food type
        # 3. Analyze packaging information

        # Placeholder return
        return {
            'detected_text': 'Sample text from image',
            'food_type': 'Detected food type',
            'packaging_info': 'Detected packaging information'
        }
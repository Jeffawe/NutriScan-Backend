import json
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
import requests
from dotenv import load_dotenv
import os
from django.shortcuts import render
from gradio_client import Client, handle_file
import spacy
import tempfile
import redis
from google import genai
from django.http import JsonResponse

# Get the directory of the current script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env from the same directory
load_dotenv(os.path.join(BASE_DIR, '.env'))

# Initialize Redis connection
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis_client = redis.from_url(redis_url)
CACHE_TTL = 60 * 60 * 24  # Cache for 24 hours

nlp = spacy.load("en_core_web_sm")

Max_Requests = int(os.getenv('MAX_REQUESTS', 30))
Max_LLM_Requests = int(os.getenv('MAX_LLM_REQUESTS', 10))


def home(request):
    return render(request, 'home.html')


class RateLimiter:
    """
    Comprehensive rate limiting class to manage different types of request limits
    """

    @staticmethod
    def get_client_ip(request):
        """
        Extract client IP address from request
        """
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip

    @staticmethod
    def should_use_bert(request):
        """
        Determine if BERT model should be used based on request count

        Returns:
            tuple: (use_bert, can_continue)
                - use_bert: Whether to switch to BERT model
                - can_continue: Whether to allow the request
        """
        # Get client IP
        ip = RateLimiter.get_client_ip(request)

        # Create unique key for this IP and endpoint
        rate_limit_key = f"rate_limit:{ip}:food_image_analysis"

        # Increment request count and get current count
        request_count = redis_client.incr(rate_limit_key)

        # Set expiration if this is the first request
        if request_count == 1:
            redis_client.expire(rate_limit_key, 3600)  # 1-hour window

        # Check thresholds
        if request_count > Max_Requests:
            # Too many requests - block
            return False, False

        # Switch to BERT after 10 requests
        return request_count > Max_LLM_Requests, True


class GlobalAPIUsageMiddleware:
    """
    Middleware to track and limit overall API usage across the application
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.TOTAL_MONTHLY_API_LIMIT = int(os.getenv('MONTHLY_API_LIMIT', 5000000))  # Total monthly API calls
        self.API_USAGE_KEY = "global_api_usage:current_month"
        if not redis_client.exists(self.API_USAGE_KEY):
            redis_client.set(self.API_USAGE_KEY, 0, ex=2592000)

    def __call__(self, request):
        # Check global API usage before processing request
        current_usage = int(redis_client.get(self.API_USAGE_KEY) or 0)

        if current_usage >= self.TOTAL_MONTHLY_API_LIMIT:
            # Set a custom attribute on the request
            request.api_limit_exceeded = True
        else:
            # Increment global API usage
            redis_client.incr(self.API_USAGE_KEY)
            request.api_limit_exceeded = False

        response = self.get_response(request)
        return response

# Create your views here.
class FoodProductView(APIView):
    def get(self, request):
        # Get Product Name from React request
        product_id = request.query_params.get('fcID')

        if not product_id:
            return Response({"error": "Missing 'fcID' parameter"}, status=400)

        # Check if result is in Redis cache
        cache_key = f"food_product:{product_id}"
        cached_result = redis_client.get(cache_key)

        if cached_result:
            # Return cached result if available
            return Response(json.loads(cached_result))

        # If not in cache, fetch from API
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

        # Cache the result in Redis
        redis_client.setex(
            cache_key,
            CACHE_TTL,
            json.dumps(processed_data)
        )

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
        # Get Product Name from React request
        product_name = request.query_params.get('name')
        page_number = int(request.query_params.get('page', 1))

        if not product_name:
            return Response({"error": "Missing 'name' parameter"}, status=400)

        # Check if result is in Redis cache
        cache_key = f"food_search:{product_name}:page:{page_number}"
        cached_result = redis_client.get(cache_key)

        if cached_result:
            # Return cached result if available
            return Response(json.loads(cached_result))

        api_key = os.getenv('USDA_API_KEY')

        headers = {
            'accept': 'application/json',
        }

        usda_api_url = 'https://api.nal.usda.gov/fdc/v1/foods/search'
        params = {
            'query': product_name,
            'pageSize': 25,
            'pageNumber': page_number,
            'dataType': 'Foundation, Branded',
            'sortBy': 'publishedDate',
            'sortOrder': 'asc',
            'api_key': api_key
        }

        response = requests.get(usda_api_url, headers=headers, params=params)
        food_data = response.json()
        result = {
            "totalPages": food_data.get('totalPages', 0),
            "data": []
        }

        for data in food_data['foods']:
            processed_data = FoodProductView.process_food_data(food_data=data)
            result["data"].append(processed_data)

        # Cache the result in Redis
        redis_client.setex(
            cache_key,
            CACHE_TTL,
            json.dumps(result)
        )

        return Response(result)


class GeminiImageAnalyzer:
    def __init__(self):
        # Initialize Redis for tracking API usage
        self.redis_client = redis.from_url(redis_url)

        # Set up Gemini API key and configuration
        self.gemini_api_key = os.getenv('GEMINI_API_KEY')
        self.client = genai.Client(api_key=self.gemini_api_key)

        # Configuration for tracking API usage
        self.MONTHLY_API_LIMIT = 1000000  # Example monthly limit in tokens/characters
        self.USAGE_WARN_THRESHOLD = 0.2  # 20% remaining

    def track_api_usage(self, tokens_used):
        """
        Track Gemini API usage and check against monthly limit

        Args:
            tokens_used (int): Number of tokens/characters used in this request

        Returns:
            bool: True if usage is within limits, False otherwise
        """
        current_usage_key = "gemini_api_usage:current_month"

        try:
            # Get current month's usage
            current_usage = int(self.redis_client.get(current_usage_key) or 0)
            new_usage = current_usage + tokens_used

            # Check if usage exceeds limit
            if new_usage > self.MONTHLY_API_LIMIT * (1 - self.USAGE_WARN_THRESHOLD):
                return False

            # Update usage
            self.redis_client.set(current_usage_key, new_usage)
            return True
        except redis.exceptions.ConnectionError as e:
            print(f"Redis connection error: {e}")
            return False

    def analyze_image(self, image_path, use_ocr=False):
        """
        Analyze image using Gemini API with fallback and usage tracking

        Args:
            image_path (str): Path to the image file
            use_ocr (bool): Whether to enable OCR-like text extraction

        Returns:
            str: Detected product name or description
        """
        try:
            # Upload the file
            uploaded_file = self.client.files.upload(file=image_path)

            # Prepare prompt
            prompt = ("Identify the food or product in this image. "
                          "Return only the product name or type. "
                          "If it's a branded product, include the brand. "
                          "Be concise and specific.")

            # Additional OCR-like text extraction if requested
            if use_ocr:
                prompt += " If there are any readable texts in the image, include them."

            # Generate response
            result = self.client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[
                    uploaded_file,
                    "\n\n",
                    prompt
                ]
            )

            tokens_used = len(result.text.split())

            # Check API usage
            if not self.track_api_usage(tokens_used):
                # Fallback to existing method if near usage limit
                return None

            print(result.text.strip())
            return result.text.strip()

        except Exception as e:
            # Log the error, fallback to existing method
            print(f"Gemini API error: {e}")
            return None

class FoodImageAnalysisView(APIView):
    parser_classes = (MultiPartParser, FormParser)
    def __init__(self, *args, **kwargs):
        super().__init__(**kwargs)
        self.gemini_analyzer = GeminiImageAnalyzer()

    def post(self, request):
        if 'image' not in request.FILES:
            return Response({'error': 'No image provided'}, status=400)

        use_bert, can_continue = RateLimiter.should_use_bert(request)

        if getattr(request, 'api_limit_exceeded', False):
            use_bert = True

        if not can_continue:
            return Response({
                'error': 'Rate limit exceeded',
                'message': 'Too many requests. Try again in 1 hour.',
                'retry_after': 3600
            }, status=429)

        image_file = request.FILES['image']
        use_OCR = request.data.get("use_OCR", "false")
        use_OCR = str(use_OCR).lower() in ["true", "1"]

        # Generate a cache key from the image content
        image_data = image_file.read()
        image_hash = hash(image_data)
        cache_key = f"food_image:{image_hash}:ocr:{use_OCR}"

        # Check cache
        cached_result = redis_client.get(cache_key)
        if cached_result:
            return Response(json.loads(cached_result))

        # Reset file pointer for further processing
        image_file.seek(0)

        # Save the uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            for chunk in image_file.chunks():
                temp_file.write(chunk)
            temp_file_path = temp_file.name

        try:
            if not use_bert:
                gemini_result = self.gemini_analyzer.analyze_image(
                    temp_file_path,
                    use_ocr=use_OCR
                )

                if gemini_result:
                    # Use Gemini result to search
                    processed_results = gemini_result
                else:
                    # Fallback to existing BERT model
                    processed_results = self.process_image(temp_file_path, use_OCR)
            else:
                processed_results = self.process_image(temp_file_path, use_OCR)

            if not processed_results:
                return Response({'error': 'No food name detected from image'}, status=400)

            food_details_response = self.get_food_details(processed_results, not use_bert)

            # Cache the result
            redis_client.setex(
                cache_key,
                CACHE_TTL * 2,  # Cache image results longer (48 hours)
                json.dumps(food_details_response)
            )

            return Response(food_details_response)
        finally:
            # Clean up the temporary file
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)

    def process_image(self, image, use_OCR):
        # Example image processing
        client = Client("Jeffawe/Food_Scanner")
        result = client.predict(
            image=handle_file(image),
            use_ocr=use_OCR,
            api_name="/recognize_image"
        )
        print(result)
        final_result = " ".join(self.extract_product_name(result))
        print(final_result)
        # Placeholder return
        return final_result

    def extract_product_name(self, text):
        if not text:
            return []

        doc = nlp(text)

        # 1. Look for brand names (usually in ALL CAPS or Title Case)
        brands = []
        for token in doc:
            if token.text.isupper() or (token.text.istitle() and len(token.text) > 2):
                # Check if part of a multi-token brand name
                if token.i < len(doc) - 1 and doc[token.i + 1].text.istitle():
                    brands.append(token.text + " " + doc[token.i + 1].text)
                else:
                    brands.append(token.text)

        # 2. Extract named entities labeled as PRODUCT or ORG
        product_ents = [ent.text for ent in doc.ents if ent.label_ in ["PRODUCT", "ORG"]]

        # 3. Look for "X of Y" patterns where Y is the product
        of_products = []
        for token in doc:
            if token.dep_ == "pobj" and token.head.text.lower() == "of":
                # Get the entire phrase starting from this token
                start_idx = token.i
                end_idx = start_idx + 1

                # Extend to include adjectives and compound nouns
                while end_idx < len(doc) and (doc[end_idx].dep_ in ["compound", "amod", "nummod"]
                                              or doc[end_idx].pos_ == "NOUN"):
                    end_idx += 1

                product_phrase = doc[start_idx:end_idx].text

                # If we have adjectives before the object
                prev_idx = token.i - 1
                while prev_idx >= 0 and doc[prev_idx].dep_ in ["amod", "compound"] and doc[prev_idx].head == token:
                    product_phrase = doc[prev_idx].text + " " + product_phrase
                    prev_idx -= 1

                of_products.append(product_phrase)

        # 4. Extract noun chunks as fallback, prioritizing food items
        container_words = ["bottle", "box", "can", "jar", "package", "container", "bag"]
        food_nouns = []
        other_nouns = []

        for chunk in doc.noun_chunks:
            # Filter out determiners and clean up the chunk
            clean_chunk = " ".join([t.text for t in chunk if t.pos_ != "DET"])

            if clean_chunk and len(clean_chunk.split()) <= 4:  # Reasonable length
                if any(container in clean_chunk.lower() for container in container_words):
                    other_nouns.append(clean_chunk)
                else:
                    food_nouns.append(clean_chunk)

        # Prioritize results
        if brands:
            # If we found brands, return them along with any "of" products
            if of_products:
                return brands + of_products
            return brands

        if of_products:
            return of_products

        if food_nouns:
            return food_nouns

        return other_nouns

    def get_food_details(self, product_name, use_llm):
        """Calls FoodProductViewMany to fetch food details."""
        if not product_name:
            return {'error': 'No valid product name found'}

        # We can reuse the cache from FoodProductViewMany if it exists
        cache_key = f"food_search:{product_name}:page:{1}"
        cached_result = redis_client.get(cache_key)

        if cached_result:
            result = json.loads(cached_result)
            result["searchTerm"] = product_name  # Add search term to the cached result
            return result

        api_key = os.getenv('USDA_API_KEY')

        headers = {
            'accept': 'application/json',
        }

        usda_api_url = 'https://api.nal.usda.gov/fdc/v1/foods/search'
        params = {
            'query': product_name,
            'pageSize': 25,
            'pageNumber': 1,
            'dataType': 'Foundation, Branded',
            'sortBy': 'publishedDate',
            'sortOrder': 'asc',
            'api_key': api_key
        }

        response = requests.get(usda_api_url, headers=headers, params=params)
        food_data = response.json()
        result = {
            "totalPages": food_data.get('totalPages', 0),
            "searchTerm": product_name,
            "data": [],
            "use_llm": use_llm
        }

        for data in food_data.get('foods', []):
            processed_data = FoodProductView.process_food_data(food_data=data)
            result['data'].append(processed_data)

        # Cache this result too
        redis_client.setex(
            cache_key,
            CACHE_TTL,
            json.dumps(result)
        )

        return result


# Add a health check endpoint for Redis
class HealthCheckView(APIView):
    def get(self, request):
        status = {
            "status": "healthy",
            "redis": "connected"
        }

        # Check Redis connection
        try:
            redis_client.ping()
        except redis.ConnectionError:
            status["status"] = "degraded"
            status["redis"] = "disconnected"

        return Response(status)
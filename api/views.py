import hashlib
import ipaddress
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import redis
import requests
from concurrent.futures import ThreadPoolExecutor
from django.shortcuts import render
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .nutrition import process_usda_food
from .openfoodfacts import (OpenFoodFactsError, fetch_product, normalize_barcode,
                            process_off_product)
from .openfoodfacts import search_products as search_off_products

# Get the directory of the current script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env from the same directory
load_dotenv(os.path.join(BASE_DIR, '.env'))

# Initialize Redis connection
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis_client = redis.from_url(redis_url)
CACHE_TTL = 60 * 60 * 24  # Cache for 24 hours

# ---- Gemini free-tier budget --------------------------------------------------------------
# Set these to the limits shown for your project at https://aistudio.google.com (Rate limits page).
# Defaults are the lowest recently reported free-tier limits (gemini-2.5-flash / 3.8-flash: 5 RPM, 20 RPD).
GEMINI_FREE_RPM = int(os.getenv('GEMINI_FREE_RPM', 5))    # requests per minute
GEMINI_FREE_RPD = int(os.getenv('GEMINI_FREE_RPD', 20))   # requests per day (resets midnight Pacific)
GEMINI_QUOTA_SHARE = float(os.getenv('GEMINI_QUOTA_SHARE', 0.5))  # use at most this share of each limit
APP_GEMINI_RPM = max(1, int(GEMINI_FREE_RPM * GEMINI_QUOTA_SHARE))
APP_GEMINI_RPD = max(1, int(GEMINI_FREE_RPD * GEMINI_QUOTA_SHARE))
# Photo scans that reach Gemini per IP per day (cached photos don't count)
MAX_IMAGE_REQUESTS_PER_IP = int(os.getenv('MAX_IMAGE_REQUESTS_PER_IP', 3))
PACIFIC = ZoneInfo('America/Los_Angeles')
MAX_BARCODE_REQUESTS = int(os.getenv('MAX_BARCODE_REQUESTS', 120))  # per IP per hour

GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.8-flash')  # 2.5-flash is closed to new users
# Tried once when the main model is overloaded (5xx) or unavailable (404). Empty to disable.
GEMINI_FALLBACK_MODEL = os.getenv('GEMINI_FALLBACK_MODEL', 'gemini-3.5-flash-lite')
# Per attempt. Worst case: 15s main + 15s fallback + 15s USDA = 45s (< gunicorn's 60s timeout)
GEMINI_TIMEOUT_MS = int(os.getenv('GEMINI_TIMEOUT_MS', 15000))
MAX_IMAGE_BYTES = int(os.getenv('MAX_IMAGE_BYTES', 10 * 1024 * 1024))  # 10 MB
# Number of public proxies in front of the app that append to X-Forwarded-For (Render: 1)
TRUSTED_PROXY_COUNT = max(1, int(os.getenv('TRUSTED_PROXY_COUNT', 1)))

USDA_SEARCH_URL = 'https://api.nal.usda.gov/fdc/v1/foods/search'
USDA_FOOD_URL = 'https://api.nal.usda.gov/fdc/v1/food/{fdc_id}'
USDA_TIMEOUT = 15  # seconds
# Foundation + SR Legacy + FNDDS cover generic/whole foods ("banana", "grilled chicken"),
# which is what image recognition usually returns; Branded covers packaged products.
USDA_DATA_TYPES = ['Foundation', 'SR Legacy', 'Survey (FNDDS)', 'Branded']

# Monthly counters are keyed by calendar month so they reset automatically
MONTH_KEY_TTL = 60 * 60 * 24 * 40  # keep a bit longer than a month, then let Redis drop it

PHOTO_UNAVAILABLE_MESSAGE = ("Photo scanning isn't available right now. Please try again later, "
                             "or scan the barcode or search by name instead.")


def month_key(prefix):
    return f"{prefix}:{datetime.now(timezone.utc):%Y-%m}"


def home(request):
    return render(request, 'home.html')


class USDAError(Exception):
    pass


class USDABusy(USDAError):
    """USDA rate-limited us (HTTP 429), or we're in the cool-down after it did."""


USDA_COOLDOWN_KEY = "usda:cooldown"
USDA_COOLDOWN_SECONDS = 60


def usda_request(method, url, **kwargs):
    """
    Call USDA with a timeout. On HTTP 429 (api.data.gov allows ~1,000 requests/hour per key)
    pause all USDA calls for a minute so we fail fast instead of piling up slow requests.
    """
    try:
        if redis_client.exists(USDA_COOLDOWN_KEY):
            raise USDABusy("USDA cool-down active")
    except redis.exceptions.RedisError:
        pass
    try:
        response = requests.request(method, url, headers={'accept': 'application/json'},
                                    timeout=USDA_TIMEOUT, **kwargs)
    except requests.RequestException as e:
        raise USDAError(str(e)) from e
    if response.status_code == 429:
        try:
            redis_client.setex(USDA_COOLDOWN_KEY, USDA_COOLDOWN_SECONDS, 1)
        except redis.exceptions.RedisError:
            pass
        raise USDABusy("USDA rate limit reached")
    return response


def usda_error_response(error):
    print(f"USDA API error: {error}")
    if isinstance(error, USDABusy):
        response = Response({"error": "Lots of people are searching right now. "
                                      "Please try again in a minute."}, status=503)
        response['Retry-After'] = str(USDA_COOLDOWN_SECONDS)
        return response
    return Response({"error": "Food database is unavailable. Please try again."}, status=502)


def _usda_search_page(query, page_number, require_all_words):
    # POST with a JSON body: USDA's GET endpoint rejects (400) a comma-separated
    # dataType list once it includes "Survey (FNDDS)" alongside other types
    body = {
        'query': query,
        'pageSize': 25,
        'pageNumber': page_number,
        'dataType': USDA_DATA_TYPES,
        'requireAllWords': require_all_words,
    }
    response = usda_request('POST', USDA_SEARCH_URL, params={'api_key': os.getenv('USDA_API_KEY')}, json=body)
    try:
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as e:
        raise USDAError(str(e)) from e


def search_usda(product_name, page_number=1):
    """
    Search USDA FoodData Central (cached). Raises USDAError on upstream failure.

    USDA matches *any* word by default, so "ambrosia peanut butter" returns every peanut butter.
    We ask for foods containing *all* the words first and only fall back to the loose search
    when nothing matches; `exactMatch` tells the caller which one happened.
    """
    normalized = product_name.strip().lower()
    cache_key = f"food_search:v2:{normalized}:page:{page_number}"
    cached_result = redis_client.get(cache_key)
    if cached_result:
        return json.loads(cached_result)

    food_data = _usda_search_page(normalized, page_number, require_all_words=True)
    exact = bool(food_data.get('foods'))
    if not exact:
        food_data = _usda_search_page(normalized, page_number, require_all_words=False)

    result = {
        "totalPages": food_data.get('totalPages', 0),
        "data": [process_usda_food(data) for data in food_data.get('foods', [])],
        "exactMatch": exact,
    }
    redis_client.setex(cache_key, CACHE_TTL, json.dumps(result))
    return result


def search_off_cached(query):
    """Open Food Facts text search (worldwide branded products), cached for a day."""
    normalized = query.strip().lower()
    cache_key = f"off_search:{normalized}"
    try:
        cached = redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except redis.exceptions.RedisError:
        pass
    results = search_off_products(normalized)
    try:
        redis_client.setex(cache_key, CACHE_TTL, json.dumps(results))
    except redis.exceptions.RedisError:
        pass
    return results


def search_foods(query, page_number=1):
    """
    Text search across both databases, run in parallel:
    - USDA (US branded + generic reference foods), paginated
    - Open Food Facts (worldwide products), first page only, shown first since they're exact brand/name hits
    If one source fails, results from the other are still returned. Raises USDAError only if
    USDA fails and Open Food Facts found nothing.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        usda_future = pool.submit(search_usda, query, page_number)
        off_future = pool.submit(search_off_cached, query) if page_number == 1 else None

        off_results = []
        if off_future is not None:
            try:
                off_results = off_future.result()
            except OpenFoodFactsError as e:
                print(f"Open Food Facts search error: {e}")

        try:
            result = dict(usda_future.result())
        except USDAError:
            if not off_results:
                raise
            result = {"totalPages": 1, "data": [], "exactMatch": False,
                      "notice": "Only showing Open Food Facts results right now (USDA is unavailable)."}

    if off_results:
        result["data"] = off_results + result["data"]
    elif not result.get("exactMatch") and result.get("data"):
        result["notice"] = (f'No exact match for "{query.strip()}". Showing the closest results instead. '
                            "Scanning the barcode is the most reliable way to find a specific product.")
    return result


def find_usda_by_barcode(barcode):
    """USDA branded foods carry a GTIN/UPC; search for it and keep only exact matches."""
    result = search_usda(barcode, 1)
    wanted = barcode.lstrip('0')
    for food in result.get('data', []):
        upc = (food['basic_info'].get('upc') or '').lstrip('0')
        if upc and upc == wanted:
            return food
    return None


class RateLimiter:
    """
    Comprehensive rate limiting class to manage different types of request limits
    """

    @staticmethod
    def get_client_ip(request):
        """
        Extract the client IP address from the request.

        The left-most X-Forwarded-For entry is whatever the client sent, so it can be faked to
        dodge rate limits. Proxies *append* to the header, so we read it from the right instead:
        skip internal/private hops, then skip TRUSTED_PROXY_COUNT - 1 public proxy hops.
        """
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if not x_forwarded_for:
            return request.META.get('REMOTE_ADDR')

        public_hops = []
        for hop in (h.strip() for h in x_forwarded_for.split(',')):
            try:
                addr = ipaddress.ip_address(hop)
            except ValueError:
                continue
            if addr.is_global:  # skips private, loopback, link-local, CGNAT and reserved ranges
                public_hops.append(hop)

        if not public_hops:
            return request.META.get('REMOTE_ADDR')

        return public_hops[max(0, len(public_hops) - TRUSTED_PROXY_COUNT)]

    @staticmethod
    def hit(request, bucket, window=3600):
        """Count a request for this IP in `bucket` and return the count within the window."""
        key = f"rate_limit:{RateLimiter.get_client_ip(request)}:{bucket}"
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, window)
        return count



class GlobalAPIUsageMiddleware:
    """
    Middleware to track and limit overall API usage across the application
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.TOTAL_MONTHLY_API_LIMIT = int(os.getenv('MONTHLY_API_LIMIT', 5000000))  # Total monthly API calls

    def __call__(self, request):
        # Key is per calendar month, so the count resets on the 1st without relying on a startup-time TTL
        usage_key = month_key("global_api_usage")
        try:
            current_usage = redis_client.incr(usage_key)
            if current_usage == 1:
                redis_client.expire(usage_key, MONTH_KEY_TTL)
            request.api_limit_exceeded = current_usage > self.TOTAL_MONTHLY_API_LIMIT
        except redis.exceptions.RedisError as e:
            # Don't take the whole API down if Redis is unavailable
            print(f"Redis error in usage middleware: {e}")
            request.api_limit_exceeded = False

        return self.get_response(request)


class FoodProductView(APIView):
    def get(self, request):
        product_id = request.query_params.get('fcID')

        if not product_id:
            return Response({"error": "Missing 'fcID' parameter"}, status=400)

        product_id = product_id.strip()
        if not product_id.isdigit():
            return Response({"error": "'fcID' must be a numeric FDC ID"}, status=400)

        cache_key = f"food_product:{product_id}"
        cached_result = redis_client.get(cache_key)
        if cached_result:
            return Response(json.loads(cached_result))

        params = {
            'format': 'full',
            'api_key': os.getenv('USDA_API_KEY')
        }

        try:
            response = usda_request('GET', USDA_FOOD_URL.format(fdc_id=product_id), params=params)
            if response.status_code == 404:
                return Response({"error": f"No food found with FDC ID {product_id}"}, status=404)
            response.raise_for_status()
            food_data = response.json()
        except USDAError as e:
            return usda_error_response(e)
        except (requests.RequestException, ValueError) as e:
            return usda_error_response(USDAError(str(e)))

        processed_data = process_usda_food(food_data)

        redis_client.setex(cache_key, CACHE_TTL, json.dumps(processed_data))

        return Response(processed_data)

    # Kept for backwards compatibility; the logic lives in api/nutrition.py
    process_food_data = staticmethod(process_usda_food)


class FoodProductViewMany(APIView):
    def get(self, request):
        product_name = (request.query_params.get('name') or '').strip()

        if not product_name:
            return Response({"error": "Missing 'name' parameter"}, status=400)

        try:
            page_number = max(1, int(request.query_params.get('page', 1)))
        except ValueError:
            return Response({"error": "'page' must be a number"}, status=400)

        try:
            result = search_foods(product_name, page_number)
        except USDAError as e:
            return usda_error_response(e)

        return Response(result)


class BarcodeLookupView(APIView):
    """
    GET /barcode/?code=<EAN/UPC>

    Looks the barcode up in Open Food Facts first (worldwide, Nutri-Score, allergens, photos),
    then falls back to USDA branded foods (US products, matched by GTIN/UPC).
    """
    NOT_FOUND_TTL = 60 * 60  # remember misses for an hour so we don't hammer upstream APIs

    def get(self, request):
        barcode = normalize_barcode(request.query_params.get('code'))
        if not barcode:
            return Response({"error": "Please provide a valid 8-14 digit barcode"}, status=400)

        cache_key = f"barcode:{barcode}"
        cached = redis_client.get(cache_key)
        if cached:
            cached = json.loads(cached)
            if cached.get('not_found'):
                return Response({"error": f"No product found for barcode {barcode}"}, status=404)
            return Response(cached)

        if RateLimiter.hit(request, 'barcode') > MAX_BARCODE_REQUESTS:
            return Response({'error': 'Rate limit exceeded',
                             'message': 'Too many barcode lookups. Try again in 1 hour.',
                             'retry_after': 3600}, status=429)

        upstream_failed = False
        result = None

        try:
            product = fetch_product(barcode)
            if product:
                result = process_off_product(product)
        except OpenFoodFactsError as e:
            print(f"Open Food Facts error: {e}")
            upstream_failed = True

        if result is None:
            try:
                result = find_usda_by_barcode(barcode)
            except USDAError as e:
                print(f"USDA API error: {e}")
                upstream_failed = True

        if result is None:
            if upstream_failed:
                # Don't cache: it may well be found once the upstream API is back
                return Response({"error": "Food databases are unavailable. Please try again."}, status=502)
            redis_client.setex(cache_key, self.NOT_FOUND_TTL, json.dumps({'not_found': True}))
            return Response({"error": f"No product found for barcode {barcode}"}, status=404)

        redis_client.setex(cache_key, CACHE_TTL, json.dumps(result))
        return Response(result)


class GeminiImageAnalyzer:
    """
    Gemini photo recognition, kept inside a share of the Gemini free-tier quota.

    The free tier limits requests per minute (RPM) and per day (RPD) for each project. Daily
    quotas reset at midnight Pacific time. We count every call in Redis *before* making it,
    across all workers and users, and stop at GEMINI_QUOTA_SHARE of each limit. If Gemini still
    answers 429 (the limits were lowered, or another app shares the key), we back off.
    """
    MINUTE_COOLDOWN = 60  # seconds to stop calling after a per-minute 429

    def __init__(self):
        self.redis_client = redis_client

        # genai.Client raises if the key is missing, so only create it when a key is configured
        self.gemini_api_key = os.getenv('GEMINI_API_KEY')
        # Timeout so a slow/hung Gemini call can't hold a server thread indefinitely
        self.client = genai.Client(
            api_key=self.gemini_api_key,
            http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
        ) if self.gemini_api_key else None

    # ---- quota accounting -------------------------------------------------------------

    @staticmethod
    def _keys():
        now_pt = datetime.now(PACIFIC)
        return (f"gemini:rpm:{now_pt:%Y%m%d%H%M}", f"gemini:rpd:{now_pt:%Y-%m-%d}")

    @staticmethod
    def _seconds_until_pacific_midnight():
        now_pt = datetime.now(PACIFIC)
        tomorrow = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(60, int((tomorrow - now_pt).total_seconds()))

    def _reserve(self):
        """Atomically claim one request from the app's share. Returns True if the call may go ahead."""
        if self.redis_client.exists("gemini:cooldown"):
            return False
        minute_key, day_key = self._keys()
        pipe = self.redis_client.pipeline()
        pipe.incr(minute_key)
        pipe.expire(minute_key, 120)
        pipe.incr(day_key)
        pipe.expire(day_key, 60 * 60 * 48)
        minute_count, _, day_count, _ = pipe.execute()
        if minute_count > APP_GEMINI_RPM or day_count > APP_GEMINI_RPD:
            # Give the slot back: this request isn't going to Gemini
            pipe = self.redis_client.pipeline()
            pipe.decr(minute_key)
            pipe.decr(day_key)
            pipe.execute()
            return False
        return True

    def _set_status(self, status):
        try:
            self.redis_client.setex("gemini:last_status", 60 * 60 * 24 * 7, status)
        except redis.exceptions.RedisError:
            pass

    def _handle_error(self, error):
        message = str(error)
        lower = message.lower()
        if getattr(error, 'code', None) == 429 or 'resource_exhausted' in lower or 'quota' in lower:
            # Daily quota exhausted -> stop until the reset; otherwise a short pause
            daily = 'per day' in lower or 'perday' in lower or 'daily' in lower
            ttl = self._seconds_until_pacific_midnight() if daily else self.MINUTE_COOLDOWN
            self.redis_client.setex("gemini:cooldown", ttl, 1)
            self._set_status("quota_exhausted")
        elif 'api key' in lower or 'api_key' in lower or 'permission' in lower:
            # An invalid key won't fix itself; don't hammer the API until it's replaced
            self.redis_client.setex("gemini:cooldown", 60 * 10, 1)
            self._set_status("invalid_or_expired_key")
        elif getattr(error, 'code', None) == 404 or 'not found' in lower or 'not_found' in lower:
            self._set_status("model_not_found")
        else:
            self._set_status("error")
        print(f"Gemini API error: {message}")

    def usage(self):
        minute_key, day_key = self._keys()
        return {
            "used_today": int(self.redis_client.get(day_key) or 0),
            "daily_limit": APP_GEMINI_RPD,
            "per_minute_limit": APP_GEMINI_RPM,
            "cooling_down": bool(self.redis_client.exists("gemini:cooldown")),
        }

    def status(self):
        """
        Health summary without calling Gemini (any API call could count against a 20/day quota).
        Reports the outcome of the most recent real call.
        """
        if self.client is None:
            return {"status": "not_configured"}
        try:
            last = self.redis_client.get("gemini:last_status")
            last = last.decode() if isinstance(last, bytes) else last
            return {"status": last or "unknown_no_calls_yet", **self.usage()}
        except redis.exceptions.RedisError:
            return {"status": "unknown"}

    # ---- recognition ------------------------------------------------------------------

    @staticmethod
    def _is_retryable(error):
        """Model overloaded (5xx / timed out) or not available (404): worth trying the fallback model."""
        code = getattr(error, 'code', None)
        lower = str(error).lower()
        return (code in (404, 500, 502, 503, 504) or 'unavailable' in lower or 'overloaded' in lower
                or 'timed out' in lower or 'timeout' in lower)

    def analyze_image(self, image_bytes, mime_type='image/jpeg', use_ocr=False):
        """
        Returns the detected food/product name, or None if Gemini is unavailable right now
        (not configured, over the app's quota share, cooling down after a 429, or an error).
        If the main model is overloaded or missing, tries GEMINI_FALLBACK_MODEL once.
        """
        if self.client is None:
            return None

        # Send the image inline instead of uploading it to the Files API
        image_part = genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        prompt = ("Identify the food or product in this image. "
                  "Return only the product name or type, in a few words suitable for "
                  "searching a nutrition database. "
                  "If it's a branded product, include the brand. "
                  "Be concise and specific. Do not add any other text.")
        if use_ocr:
            prompt += " If there are any readable texts in the image, include them."

        models = [GEMINI_MODEL] + ([GEMINI_FALLBACK_MODEL] if GEMINI_FALLBACK_MODEL
                                   and GEMINI_FALLBACK_MODEL != GEMINI_MODEL else [])
        for attempt, model in enumerate(models):
            try:
                # Every attempt counts against the app's share of the free tier
                if not self._reserve():
                    return None
            except redis.exceptions.RedisError as e:
                # Without Redis we can't count usage, so don't risk the shared quota
                print(f"Redis error, skipping Gemini: {e}")
                return None

            try:
                result = self.client.models.generate_content(
                    model=model,
                    contents=[image_part, "\n\n", prompt]
                )
                text = (result.text or '').strip()
                self._set_status("ok" if attempt == 0 else "ok_fallback")
                print(f"Gemini ({model}) identified: {text}")
                return text or None

            except Exception as e:
                if attempt < len(models) - 1 and self._is_retryable(e):
                    print(f"Gemini ({model}) unavailable, trying fallback: {e}")
                    continue
                try:
                    self._handle_error(e)
                except redis.exceptions.RedisError:
                    print(f"Gemini API error: {e}")
                return None
        return None


class FoodImageAnalysisView(APIView):
    """Photo -> food name (Gemini) -> USDA search."""
    parser_classes = (MultiPartParser, FormParser)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gemini_analyzer = GeminiImageAnalyzer()

    def post(self, request):
        if 'image' not in request.FILES:
            return Response({'error': 'No image provided'}, status=400)

        image_file = request.FILES['image']

        if image_file.size > MAX_IMAGE_BYTES:
            return Response({'error': f'Image too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)'}, status=413)

        mime_type = image_file.content_type or 'image/jpeg'
        if not mime_type.startswith('image/'):
            return Response({'error': 'Uploaded file is not an image'}, status=400)

        use_OCR = str(request.data.get("use_OCR", "false")).lower() in ["true", "1"]

        # Stable cache key from the image content.
        # (Python's built-in hash() is randomized per process, so it can't be used across workers/restarts.)
        image_data = image_file.read()
        image_hash = hashlib.sha256(image_data).hexdigest()
        cache_key = f"food_image:{image_hash}:ocr:{use_OCR}"

        # Cached photos don't count against any limit or cost a Gemini call
        cached_result = redis_client.get(cache_key)
        if cached_result:
            return Response(json.loads(cached_result))

        if getattr(request, 'api_limit_exceeded', False):
            return Response({'error': PHOTO_UNAVAILABLE_MESSAGE}, status=503)

        # Small per-person cap so one visitor can't use up the whole day's shared Gemini quota
        if RateLimiter.hit(request, 'food_image_analysis', window=60 * 60 * 24) > MAX_IMAGE_REQUESTS_PER_IP:
            return Response({
                'error': 'Rate limit exceeded',
                'message': "You've used today's photo scans. Try again tomorrow, "
                           "or scan the barcode or search by name instead.",
            }, status=429)

        food_name = self.gemini_analyzer.analyze_image(image_data, mime_type=mime_type, use_ocr=use_OCR)
        if not food_name:
            # Not configured, quota share used up, cooling down after a 429, or the call failed
            return Response({'error': PHOTO_UNAVAILABLE_MESSAGE}, status=503)

        try:
            food_details_response = self.get_food_details(food_name)
        except USDAError as e:
            return usda_error_response(e)

        redis_client.setex(
            cache_key,
            CACHE_TTL * 2,  # Cache image results longer (48 hours)
            json.dumps(food_details_response)
        )

        return Response(food_details_response)

    def get_food_details(self, product_name):
        """Search USDA for the detected name (shares the search cache with FoodProductViewMany)."""
        result = dict(search_foods(product_name, 1))
        result["searchTerm"] = product_name
        return result


class HealthCheckView(APIView):
    def get(self, request):
        status = {
            "status": "healthy",
            "redis": "connected",
        }

        try:
            redis_client.ping()
        except redis.exceptions.RedisError:
            status["status"] = "degraded"
            status["redis"] = "disconnected"

        # Last real Gemini outcome + today's usage; never calls Gemini itself
        status["gemini"] = GeminiImageAnalyzer().status()

        return Response(status)

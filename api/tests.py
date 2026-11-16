from unittest import mock

from django.test import RequestFactory, SimpleTestCase

from . import views
from .nutrition import find_allergens, process_usda_food
from .openfoodfacts import normalize_barcode, process_off_product


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def decr(self, key):
        self.store[key] = int(self.store.get(key, 0)) - 1
        return self.store[key]

    def exists(self, key):
        return int(key in self.store)

    def expire(self, key, ttl):
        pass

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def __getattr__(self, name):
        def queue(*args):
            self.ops.append((name, args))
            return self
        return queue

    def execute(self):
        results = [getattr(self.redis, name)(*args) for name, args in self.ops]
        self.ops = []
        return results


def fake_response(json_data, status_code=200):
    response = mock.Mock(status_code=status_code)
    response.json.return_value = json_data
    response.raise_for_status = mock.Mock()
    return response


class UsdaProcessingTests(SimpleTestCase):
    def test_kcal_wins_over_kilojoules(self):
        food = {'foodNutrients': [
            {'nutrientName': 'Energy', 'value': 250, 'unitName': 'KCAL'},
            {'nutrientName': 'Energy', 'value': 1046, 'unitName': 'kJ'},
        ]}
        self.assertEqual(process_usda_food(food)['basic_info']['calories'], 250)

    def test_foundation_atwater_energy(self):
        food = {'foodNutrients': [
            {'nutrient': {'name': 'Energy (Atwater General Factors)', 'unitName': 'kcal'}, 'amount': 89},
            {'nutrient': {'name': 'Energy (Atwater Specific Factors)', 'unitName': 'kcal'}, 'amount': 85},
        ]}
        self.assertEqual(process_usda_food(food)['basic_info']['calories'], 89)

    def test_macro_split(self):
        food = {'foodNutrients': [
            {'nutrientName': 'Energy', 'value': 100, 'unitName': 'KCAL'},
            {'nutrientName': 'Total lipid (fat)', 'value': 5, 'unitName': 'G'},
            {'nutrientName': 'Protein', 'value': 10, 'unitName': 'G'},
        ]}
        profile = process_usda_food(food)['analysis']['nutritional_profile']
        self.assertEqual(profile['fat_calories_percent'], 45.0)
        self.assertEqual(profile['protein_calories_percent'], 40.0)


class AllergenTests(SimpleTestCase):
    def test_word_boundaries(self):
        self.assertEqual(find_allergens('Eggplant, tomato, salt'), [])
        self.assertIn('egg', find_allergens('Water, eggs, sugar'))

    def test_hidden_milk_ingredients(self):
        self.assertIn('milk', find_allergens('Sugar, whey powder, sodium caseinate'))

    def test_peanut_butter_is_not_dairy(self):
        found = find_allergens('Peanut butter, cocoa butter')
        self.assertIn('peanut', found)
        self.assertNotIn('milk', found)


class OpenFoodFactsTests(SimpleTestCase):
    PRODUCT = {
        'code': '3017620422003',
        'product_name': 'Nutella',
        'brands': 'Ferrero, Nutella',
        'categories': 'Spreads, Sweet spreads, Hazelnut spreads',
        'ingredients_text': 'Sugar, palm oil, hazelnuts 13%, skimmed milk powder 8.7%, cocoa',
        'nutriments': {'energy-kcal_100g': 539, 'proteins_100g': 6.3, 'fat_100g': 30.9,
                       'carbohydrates_100g': 57.5, 'sugars_100g': 56.3, 'sodium_100g': 0.0428,
                       'calcium_100g': 0.108},
        'nutriscore_grade': 'e',
        'nova_group': 4,
        'allergens_tags': ['en:milk', 'en:nuts', 'en:soybeans'],
        'traces_tags': ['en:gluten'],
        'additives_tags': ['en:e322', 'en:e322i'],
        'image_front_url': 'https://images.openfoodfacts.org/x.jpg',
    }

    def test_normalizes_product(self):
        result = process_off_product(self.PRODUCT)
        info, analysis = result['basic_info'], result['analysis']
        self.assertEqual(info['name'], 'Nutella')
        self.assertEqual(info['brand'], 'Ferrero')
        self.assertEqual(info['calories'], 539)
        self.assertEqual(info['source'], 'openfoodfacts')
        self.assertEqual(analysis['allergens'], ['milk', 'tree nut', 'soy'])
        self.assertEqual(analysis['traces'], ['gluten'])
        self.assertEqual(analysis['nutri_score'], 'e')
        self.assertEqual(analysis['nova_group'], 4)
        self.assertEqual(analysis['key_highlights'][0], 'Nutri-Score E')
        self.assertEqual(info['category'], 'Hazelnut spreads')

    def test_partial_english_ingredients_fall_back_to_original(self):
        product = dict(self.PRODUCT, ingredients_text_en='sugar, palm oil',
                       ingredients_text='Sucre, huile de palme, NOISETTES 13%, cacao maigre 7,4%, LAIT écrémé',
                       categories='Spreads, fr:Nutella')
        info = process_off_product(product)['basic_info']
        self.assertTrue(info['ingredients'].startswith('Sucre'))
        self.assertEqual(info['category'], 'Spreads')

    def test_units_converted_from_grams(self):
        minerals = {n['name']: n for n in process_off_product(self.PRODUCT)['nutrients']['minerals']}
        self.assertEqual(minerals['Sodium']['amount'], 42.8)
        self.assertEqual(minerals['Sodium']['unit'], 'MG')
        self.assertEqual(minerals['Calcium']['amount'], 108.0)

    def test_kj_only_energy(self):
        product = {'nutriments': {'energy_100g': 418.4}}
        self.assertEqual(process_off_product(product)['basic_info']['calories'], 100.0)

    def test_normalize_barcode(self):
        self.assertEqual(normalize_barcode(' 0 12345 67890 5 '), '012345678905')
        self.assertIsNone(normalize_barcode('123'))
        self.assertIsNone(normalize_barcode('abc'))


class ClientIpTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def ip(self, xff, remote='10.0.0.1'):
        request = self.factory.get('/', HTTP_X_FORWARDED_FOR=xff, REMOTE_ADDR=remote)
        return views.RateLimiter.get_client_ip(request)

    def test_spoofed_left_entry_is_ignored(self):
        # client sent "8.8.4.4" itself; the proxy appended the real address
        self.assertEqual(self.ip('8.8.4.4, 9.9.9.9'), '9.9.9.9')

    def test_internal_hops_are_skipped(self):
        self.assertEqual(self.ip('9.9.9.9, 10.1.2.3'), '9.9.9.9')

    def test_garbage_header_uses_remote_addr(self):
        self.assertEqual(self.ip('not-an-ip', remote='9.9.9.9'), '9.9.9.9')

    def test_no_header_uses_remote_addr(self):
        request = self.factory.get('/', REMOTE_ADDR='9.9.9.9')
        self.assertEqual(views.RateLimiter.get_client_ip(request), '9.9.9.9')


class BarcodeViewTests(SimpleTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patcher = mock.patch.object(views, 'redis_client', self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.view = views.BarcodeLookupView.as_view()
        self.factory = RequestFactory()

    def get(self, code):
        return self.view(self.factory.get('/barcode/', {'code': code}))

    def test_invalid_barcode(self):
        self.assertEqual(self.get('12').status_code, 400)

    def test_found_in_open_food_facts(self):
        off = fake_response({'status': 1, 'product': OpenFoodFactsTests.PRODUCT})
        with mock.patch('api.openfoodfacts.requests.get', return_value=off):
            response = self.get('3017620422003')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['basic_info']['name'], 'Nutella')
        # second request is served from cache
        with mock.patch('api.openfoodfacts.requests.get') as off_get:
            self.assertEqual(self.get('3017620422003').status_code, 200)
            off_get.assert_not_called()

    def test_falls_back_to_usda_upc_match(self):
        off_missing = fake_response({'status': 0}, status_code=404)
        usda = fake_response({'totalPages': 1, 'foods': [
            {'description': 'OTHER', 'gtinUpc': '999999999999', 'foodNutrients': []},
            {'description': 'CHEERIOS', 'gtinUpc': '016000275287', 'foodNutrients': []},
        ]})
        with mock.patch('api.openfoodfacts.requests.get', return_value=off_missing), \
                mock.patch('api.views.requests.request', return_value=usda):
            response = self.get('0016000275287')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['basic_info']['name'], 'CHEERIOS')

    def test_not_found(self):
        off_missing = fake_response({'status': 0}, status_code=404)
        usda_empty = fake_response({'totalPages': 0, 'foods': []})
        with mock.patch('api.openfoodfacts.requests.get', return_value=off_missing), \
                mock.patch('api.views.requests.request', return_value=usda_empty):
            self.assertEqual(self.get('4006381333931').status_code, 404)


class ImageAnalysisViewTests(SimpleTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patcher = mock.patch.object(views, 'redis_client', self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.factory = RequestFactory()

    def post_image(self, content=b'fake-jpeg-bytes', content_type='image/jpeg', ip='9.9.9.9'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        upload = SimpleUploadedFile('food.jpg', content, content_type=content_type)
        request = self.factory.post('/analyze-image/', {'image': upload}, REMOTE_ADDR=ip)
        return views.FoodImageAnalysisView.as_view()(request)

    def test_no_gemini_key_returns_friendly_503(self):
        with mock.patch.dict('os.environ', {'GEMINI_API_KEY': ''}):
            response = self.post_image()
        self.assertEqual(response.status_code, 503)
        self.assertIn('try again later', response.data['error'])

    def test_rejects_non_images(self):
        self.assertEqual(self.post_image(content_type='text/plain').status_code, 400)

    def test_gemini_result_is_searched_and_cached(self):
        usda = fake_response({'totalPages': 1, 'foods': [
            {'description': 'BANANA', 'foodNutrients': []}]})
        with mock.patch.object(views.GeminiImageAnalyzer, 'analyze_image', return_value='banana') as gemini, \
                mock.patch('api.views.requests.request', return_value=usda):
            first = self.post_image()
            second = self.post_image()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data['searchTerm'], 'banana')
        self.assertEqual(second.status_code, 200)
        gemini.assert_called_once()  # same photo again is served from cache

    def test_per_ip_daily_limit(self):
        with mock.patch.object(views.GeminiImageAnalyzer, 'analyze_image', return_value=None), \
                mock.patch.object(views, 'MAX_IMAGE_REQUESTS_PER_IP', 2):
            codes = [self.post_image(content=f'img{i}'.encode()).status_code for i in range(3)]
            other_person = self.post_image(content=b'other', ip='8.8.8.8').status_code
        self.assertEqual(codes, [503, 503, 429])
        self.assertEqual(other_person, 503)  # a different IP isn't blocked


class GeminiQuotaTests(SimpleTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patcher = mock.patch.object(views, 'redis_client', self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.dict('os.environ', {'GEMINI_API_KEY': 'test-key'}):
            self.analyzer = views.GeminiImageAnalyzer()
        self.analyzer.client = mock.Mock()
        self.analyzer.client.models.generate_content.return_value = mock.Mock(text='banana')

    def test_stops_at_daily_share(self):
        with mock.patch.object(views, 'APP_GEMINI_RPD', 3), mock.patch.object(views, 'APP_GEMINI_RPM', 100):
            results = [self.analyzer.analyze_image(b'x') for _ in range(5)]
        self.assertEqual(results, ['banana'] * 3 + [None] * 2)
        self.assertEqual(self.analyzer.client.models.generate_content.call_count, 3)
        self.assertEqual(self.analyzer.usage()['used_today'], 3)  # refused calls are given back

    def test_stops_at_per_minute_share(self):
        with mock.patch.object(views, 'APP_GEMINI_RPD', 100), mock.patch.object(views, 'APP_GEMINI_RPM', 2):
            results = [self.analyzer.analyze_image(b'x') for _ in range(3)]
        self.assertEqual(results, ['banana', 'banana', None])

    def test_429_from_gemini_triggers_cooldown(self):
        error = Exception('429 RESOURCE_EXHAUSTED: quota exceeded for requests per day')
        self.analyzer.client.models.generate_content.side_effect = error
        self.assertIsNone(self.analyzer.analyze_image(b'x'))
        self.analyzer.client.models.generate_content.side_effect = None
        self.assertIsNone(self.analyzer.analyze_image(b'x'))  # cooling down: Gemini not called again
        self.assertEqual(self.analyzer.client.models.generate_content.call_count, 1)
        self.assertEqual(self.analyzer.status()['status'], 'quota_exhausted')

    def test_default_share_is_half_of_free_tier(self):
        self.assertEqual(views.APP_GEMINI_RPD, int(views.GEMINI_FREE_RPD * 0.5))
        self.assertEqual(views.APP_GEMINI_RPM, int(views.GEMINI_FREE_RPM * 0.5))


class UsdaBusyTests(SimpleTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patcher = mock.patch.object(views, 'redis_client', self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.view = views.FoodProductViewMany.as_view()
        self.factory = RequestFactory()

    def test_429_returns_busy_and_pauses_usda(self):
        with mock.patch('api.views.requests.request', return_value=fake_response({}, status_code=429)) as usda:
            first = self.view(self.factory.get('/analyze-many-food/', {'name': 'apple'}))
            second = self.view(self.factory.get('/analyze-many-food/', {'name': 'pear'}))
        self.assertEqual(first.status_code, 503)
        self.assertEqual(first['Retry-After'], '60')
        self.assertEqual(second.status_code, 503)
        usda.assert_called_once()  # second request didn't hit USDA during the cool-down


class GeminiModelNotFoundTests(GeminiQuotaTests):
    def test_404_reports_model_not_found(self):
        error = Exception("404 NOT_FOUND. This model models/gemini-2.5-flash is no longer available")
        error.code = 404
        self.analyzer.client.models.generate_content.side_effect = error
        self.assertIsNone(self.analyzer.analyze_image(b'x'))
        self.assertEqual(self.analyzer.status()['status'], 'model_not_found')


class GeminiFallbackTests(GeminiQuotaTests):
    def test_overloaded_main_model_falls_back(self):
        overloaded = Exception('503 UNAVAILABLE. This model is currently experiencing high demand.')
        overloaded.code = 503
        self.analyzer.client.models.generate_content.side_effect = [overloaded, mock.Mock(text='nutella')]
        self.assertEqual(self.analyzer.analyze_image(b'x'), 'nutella')
        models_tried = [c.kwargs['model'] for c in self.analyzer.client.models.generate_content.call_args_list]
        self.assertEqual(models_tried, [views.GEMINI_MODEL, views.GEMINI_FALLBACK_MODEL])
        self.assertEqual(self.analyzer.usage()['used_today'], 2)  # both attempts counted
        self.assertEqual(self.analyzer.status()['status'], 'ok_fallback')

    def test_quota_error_does_not_fall_back(self):
        quota = Exception('429 RESOURCE_EXHAUSTED: quota exceeded')
        quota.code = 429
        self.analyzer.client.models.generate_content.side_effect = quota
        self.assertIsNone(self.analyzer.analyze_image(b'x'))
        self.assertEqual(self.analyzer.client.models.generate_content.call_count, 1)

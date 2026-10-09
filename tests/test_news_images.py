import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pydantic import ValidationError

from app.api.v1.endpoints import news
from app.models.news import News
from app.schemas.news import NewsCreate, NewsRead, NewsUpdate


URLS = [f"https://images.example.test/{i}.png" for i in range(5)]
ADMIN = SimpleNamespace(user_id="11111111-1111-4111-8111-111111111111")
NOW = datetime.now(timezone.utc)


def article(images=None, cover=None):
    return News(id=1, title="News", body="<b>Details</b>", image_urls=images or [],
                image_url=cover, is_published=False, published_at=None,
                created_at=NOW, updated_at=NOW)


def database(item=None):
    results = [SimpleNamespace(scalar_one_or_none=lambda: item), SimpleNamespace(scalar=lambda: 3)]

    async def refresh(value):
        value.id = value.id or 1
        value.created_at = NOW
        value.updated_at = NOW
        value.published_at = NOW if value.is_published else None

    return SimpleNamespace(execute=AsyncMock(side_effect=results), add=Mock(),
                           flush=AsyncMock(), refresh=AsyncMock(side_effect=refresh))


class NewsImageSchemaTests(unittest.TestCase):
    def test_five_images_and_manual_cover_preserve_order(self):
        value = NewsCreate(title="News", image_urls=URLS, image_url=URLS[3])
        self.assertEqual(value.image_urls, URLS)
        self.assertEqual(value.image_url, URLS[3])

    def test_legacy_create_initializes_gallery(self):
        value = NewsCreate(title="News", image_url=URLS[0])
        self.assertEqual(value.image_urls, [URLS[0]])
        self.assertIsNone(NewsCreate(title="No image").image_url)
        self.assertEqual(NewsCreate(title="No image").image_urls, [])

    def test_first_image_is_default_cover(self):
        value = NewsCreate(title="News", image_urls=URLS)
        self.assertEqual(value.image_url, URLS[0])

    def test_more_than_five_images_cannot_bypass_admin_ui(self):
        for schema in (NewsCreate, NewsUpdate):
            with self.subTest(schema=schema), self.assertRaises(ValidationError):
                schema(title="News", image_urls=URLS + ["https://images.example.test/6.png"])

    def test_invalid_urls_duplicates_and_unrelated_cover_are_rejected(self):
        invalid = [
            {"image_urls": ["javascript:alert(1)"]},
            {"image_urls": ["file:///private.png"]},
            {"image_url": "data:image/png;base64,aaa"},
            {"image_urls": [URLS[0], URLS[0]]},
            {"image_urls": URLS, "image_url": "https://other.example.test/cover.png"},
            {"image_urls": [], "image_url": URLS[0]},
        ]
        for schema in (NewsCreate, NewsUpdate):
            for fields in invalid:
                with self.subTest(schema=schema, fields=fields), self.assertRaises(ValidationError):
                    schema(title="News", **fields)

    def test_legacy_read_includes_gallery_and_cover(self):
        item = article(cover=URLS[0])
        item.image_urls = None
        value = NewsRead.model_validate(item)
        self.assertEqual(value.image_urls, [URLS[0]])
        self.assertEqual(value.image_url, URLS[0])


class NewsImageEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_persists_all_images_and_selected_cover(self):
        db = database()
        value = await news.create_news(NewsCreate(title="News", image_urls=URLS, image_url=URLS[2]), ADMIN, db)
        stored = db.add.call_args.args[0]
        self.assertEqual(stored.image_urls, URLS)
        self.assertEqual(stored.image_url, URLS[2])
        self.assertEqual(value.image_urls, URLS)
        db.flush.assert_awaited_once()

    async def test_removing_cover_selects_remaining_image(self):
        item = article(URLS, URLS[2])
        result = await news.update_news(1, NewsUpdate(image_urls=URLS[:2]), ADMIN, database(item))
        self.assertEqual(result.image_urls, URLS[:2])
        self.assertEqual(result.image_url, URLS[0])

    async def test_changing_only_cover_keeps_gallery(self):
        item = article(URLS, URLS[0])
        result = await news.update_news(1, NewsUpdate(image_url=URLS[4]), ADMIN, database(item))
        self.assertEqual(result.image_urls, URLS)
        self.assertEqual(result.image_url, URLS[4])

    async def test_clearing_gallery_also_clears_cover(self):
        item = article(URLS, URLS[0])
        result = await news.update_news(1, NewsUpdate(image_urls=[], image_url=None), ADMIN, database(item))
        self.assertEqual(result.image_urls, [])
        self.assertIsNone(result.image_url)

    async def test_legacy_update_replaces_or_removes_single_image(self):
        item = article(URLS[:2], URLS[0])
        result = await news.update_news(1, NewsUpdate(image_url=URLS[4]), ADMIN, database(item))
        self.assertEqual(result.image_urls, [URLS[4]])
        result = await news.update_news(1, NewsUpdate(image_url=None), ADMIN, database(item))
        self.assertEqual(result.image_urls, [])
        self.assertIsNone(result.image_url)

    async def test_publish_or_text_patch_does_not_touch_images(self):
        item = article(URLS, URLS[4])
        result = await news.update_news(1, NewsUpdate(title="Updated", is_published=True), ADMIN, database(item))
        self.assertEqual(result.image_urls, URLS)
        self.assertEqual(result.image_url, URLS[4])
        self.assertEqual(result.body, "<b>Details</b>")

    def test_write_routes_remain_admin_only(self):
        for endpoint in (news.create_news, news.update_news, news.upload_news_image):
            route = next(route for route in news.router.routes if route.endpoint is endpoint)
            self.assertIn(news.require_admin, [dependency.call for dependency in route.dependant.dependencies])

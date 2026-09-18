"""Offline unit tests for `src.pipeline.image_sourcing` (plan F.5).

Fully mocked httpx — no network, no browser, no LLM. Image fixtures are
real PIL-encoded bytes so the pixel gates run against a genuine decoder
(HTML width/height attributes are never trusted, so the tests must feed
real encoded images too).

Covers the Supplier-First rewrite: images come ONLY from the verified
`RawSupplierProduct.image_urls` CDN array, validated deterministically,
with a hard 3-distinct-image minimum, URL/byte-hash dedupe, and no
fallback tier to any other site.
"""

import os
from io import BytesIO

import httpx
import pytest
from PIL import Image

from src.models import RawSupplierProduct
from src.pipeline.image_sourcing import (
    MAX_GALLERY_IMAGES,
    MIN_DISTINCT_IMAGES,
    download_and_validate_image,
    image_extension,
    make_download_client,
    source_product_images,
    strip_size_suffix,
)

_ALI_URL = "https://www.aliexpress.com/item/1005006112233445.html"


# ----------------------------------------------------------------------
# Real encoded image fixtures (cached — noise JPEGs are expensive)
# ----------------------------------------------------------------------

_blob_cache = {}


def _image_bytes(width: int, height: int, fmt: str = "JPEG") -> bytes:
    """Incompressible random-noise image of the requested size/format."""
    key = (width, height, fmt)
    if key not in _blob_cache:
        img = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
        buf = BytesIO()
        if fmt == "JPEG":
            img.save(buf, format="JPEG", quality=85)
        else:
            img.save(buf, format=fmt)
        _blob_cache[key] = buf.getvalue()
    return _blob_cache[key]


def _valid_square_jpeg() -> bytes:
    return _image_bytes(850, 850, "JPEG")


def _vertical_jpeg() -> bytes:
    # 9:16 vertical — a legitimate supplier product photo.
    return _image_bytes(1080, 1920, "JPEG")


def _small_jpeg() -> bytes:
    # 400x400 noise: decodes fine but is far below the 800px gate.
    return _image_bytes(400, 400, "JPEG")


class FakeResponse:
    def __init__(self, status_code=200, content=b"", content_type="image/jpeg"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in: records gets, serves canned data."""

    def __init__(self):
        self.responses = {}
        self.fail_urls = set()
        self.requested = []
        self.closed = False

    def serve(self, url: str, content: bytes, content_type: str = "image/jpeg"):
        self.responses[url] = FakeResponse(content=content, content_type=content_type)
        return self

    def fail(self, *urls):
        self.fail_urls.update(urls)
        return self

    async def get(self, url):
        self.requested.append(url)
        if url in self.fail_urls:
            raise httpx.ConnectError(f"refused: {url}")
        if url in self.responses:
            return self.responses[url]
        return FakeResponse()  # default: empty 200 — fails validation

    async def aclose(self):
        self.closed = True


def _product(image_urls, **overrides) -> RawSupplierProduct:
    base = dict(
        supplier_name="AliExpress",
        supplier_retail_url=_ALI_URL,
        product_title="Cable organiser",
        product_description="A spine.",
        price_aud=12.50,
        shipping_cost_aud=0.0,
        image_urls=list(image_urls),
    )
    base.update(overrides)
    return RawSupplierProduct(**base)


# ----------------------------------------------------------------------
# URL upgrade + format helpers
# ----------------------------------------------------------------------


def test_strip_size_suffix_requests_the_original_alicdn_asset():
    suffixed = "https://ae01.alicdn.com/kf/S123456789.jpg_960x960q75.jpg_.avif"
    assert strip_size_suffix(suffixed) == "https://ae01.alicdn.com/kf/S123456789.jpg"


def test_strip_size_suffix_strips_alicdn_double_extension_thumbnails():
    legacy = "https://ae01.alicdn.com/kf/S123.jpg_640x640.jpg"
    assert strip_size_suffix(legacy) == "https://ae01.alicdn.com/kf/S123.jpg"


def test_strip_size_suffix_strips_shopify_size_markers():
    shopify = "https://cdn.shopify.com/s/files/1/0/x/products/photo_800x800.jpg"
    assert (
        strip_size_suffix(shopify)
        == "https://cdn.shopify.com/s/files/1/0/x/products/photo.jpg"
    )


def test_strip_size_suffix_leaves_clean_urls_untouched():
    clean = "https://ae01.alicdn.com/kf/S123.jpg"
    assert strip_size_suffix(clean) == clean


def test_strip_size_suffix_also_upgrades_alicdn_crop_variants():
    # alicdn crop variants carry the same mid-filename marker shape; the
    # original full asset is everything before the marker.
    crop = "https://ae01.alicdn.com/kf/S123.jpg_350x350x50x50.jpg"
    assert strip_size_suffix(crop) == "https://ae01.alicdn.com/kf/S123.jpg"


def test_image_extension_follows_the_encoded_format():
    assert image_extension(_valid_square_jpeg()) == ".jpg"
    assert image_extension(_image_bytes(850, 850, "PNG")) == ".png"


def test_min_distinct_images_is_three():
    assert MIN_DISTINCT_IMAGES == 3


def test_max_gallery_images_cap():
    assert MAX_GALLERY_IMAGES == 8


async def test_make_download_client_uses_the_browser_user_agent():
    from src.config import settings

    client = make_download_client()
    try:
        assert client.headers["user-agent"] == settings.USER_AGENT
        assert client.follow_redirects is True
    finally:
        await client.aclose()


# ----------------------------------------------------------------------
# download_and_validate_image gates
# ----------------------------------------------------------------------


async def test_download_accepts_a_clean_product_jpeg():
    client = FakeAsyncClient().serve(
        "https://cdn.example.com/p.jpg", _valid_square_jpeg()
    )
    data = await download_and_validate_image("https://cdn.example.com/p.jpg", client=client)
    assert data == _valid_square_jpeg()


async def test_download_accepts_vertical_product_photos():
    client = FakeAsyncClient().serve(
        "https://cdn.example.com/vertical.jpg", _vertical_jpeg()
    )
    data = await download_and_validate_image(
        "https://cdn.example.com/vertical.jpg", client=client
    )
    assert data == _vertical_jpeg()


async def test_download_accepts_content_type_with_charset_parameter():
    client = FakeAsyncClient().serve(
        "https://cdn.example.com/p.jpg", _valid_square_jpeg(),
        content_type="image/jpeg; charset=utf-8",
    )
    assert await download_and_validate_image(
        "https://cdn.example.com/p.jpg", client=client
    )


async def test_download_accepts_webp():
    webp = _image_bytes(850, 850, "WEBP")
    client = FakeAsyncClient().serve(
        "https://cdn.example.com/p.webp", webp, content_type="image/webp"
    )
    assert await download_and_validate_image(
        "https://cdn.example.com/p.webp", client=client
    ) == webp


async def test_download_rejects_every_bad_candidate():
    bad = [
        ("https://cdn.example.com/404.jpg", lambda c: c, 404, "image/jpeg"),
        ("https://cdn.example.com/html.jpg", lambda c: c, 200, "text/html"),
        ("https://cdn.example.com/avif.jpg", lambda c: c, 200, "image/avif"),
    ]
    for url, _, status, content_type in bad:
        client = FakeAsyncClient().serve(
            url, _valid_square_jpeg(), content_type=content_type
        )
        if status != 200:
            client.responses[url].status_code = status
        assert await download_and_validate_image(url, client=client) is None, url


async def test_download_rejects_undersized_pixels():
    client = FakeAsyncClient().serve(
        "https://cdn.example.com/small.jpg", _small_jpeg()
    )
    assert await download_and_validate_image(
        "https://cdn.example.com/small.jpg", client=client
    ) is None


async def test_download_rejects_undersized_bytes():
    client = FakeAsyncClient().serve(
        "https://cdn.example.com/icon.jpg", b"\x00" * 500, content_type="image/jpeg"
    )
    assert await download_and_validate_image(
        "https://cdn.example.com/icon.jpg", client=client
    ) is None


async def test_download_rejects_transport_failures():
    client = FakeAsyncClient().fail("https://cdn.example.com/dead.jpg")
    assert await download_and_validate_image(
        "https://cdn.example.com/dead.jpg", client=client
    ) is None


# ----------------------------------------------------------------------
# source_product_images (the F.5 public entry)
# ----------------------------------------------------------------------


async def test_sources_three_distinct_valid_images():
    urls = [
        "https://cdn.example.com/1.jpg",
        "https://cdn.example.com/2.jpg",
        "https://cdn.example.com/3.jpg",
    ]
    client = FakeAsyncClient()
    for i, url in enumerate(urls):
        client.serve(url, _image_bytes(850 + i, 850, "JPEG"))
    blobs = await source_product_images(_product(urls), client=client)
    assert blobs is not None
    assert len(blobs) == 3


async def test_fewer_than_three_valid_images_returns_none():
    urls = [
        "https://cdn.example.com/1.jpg",
        "https://cdn.example.com/broken-1.jpg",
        "https://cdn.example.com/broken-2.jpg",
    ]
    client = FakeAsyncClient().serve(urls[0], _valid_square_jpeg())
    client.fail(urls[1], urls[2])
    assert await source_product_images(_product(urls), client=client) is None


async def test_size_suffixed_gallery_urls_are_upgraded_original_first():
    original = "https://ae01.alicdn.com/kf/S123456789.jpg"
    as_served = original + "_960x960q75.jpg_.avif"
    other = ["https://ae01.alicdn.com/kf/S222.jpg", "https://ae01.alicdn.com/kf/S333.jpg"]
    client = FakeAsyncClient().serve(original, _valid_square_jpeg())
    client.serve(other[0], _image_bytes(850, 860))
    client.serve(other[1], _image_bytes(850, 870))
    product = _product([as_served] + other)
    blobs = await source_product_images(product, client=client)
    assert blobs is not None and len(blobs) == 3
    # The upgraded original was requested before the as-served thumbnail.
    assert client.requested[0] == original
    assert client.requested[1] == as_served


async def test_duplicate_images_served_from_two_urls_count_once():
    base = "https://cdn.example.com"
    client = FakeAsyncClient()
    # gallery1/1.jpg and gallery2/1.jpg serve identical bytes (same as the
    # size-upgrade pairing): both URLs are tried, only one blob survives.
    client.serve(f"{base}/gallery1/1.jpg", _valid_square_jpeg())
    client.serve(f"{base}/gallery2/1.jpg", _valid_square_jpeg())
    client.serve(f"{base}/gallery2/2.jpg", _small_jpeg())
    product = _product([
        f"{base}/gallery1/1.jpg",
        f"{base}/gallery1/1_640x640.jpg",
        f"{base}/gallery2/1.jpg",
        f"{base}/gallery2/2.jpg",
    ])
    assert await source_product_images(product, client=client) is None


async def test_owned_client_is_closed_when_injected_client_is_omitted(monkeypatch):
    class TrackingClient(FakeAsyncClient):
        instances = []

        def __init__(self):
            super().__init__()
            TrackingClient.instances.append(self)

    client = TrackingClient().serve(
        "https://cdn.example.com/1.jpg", _valid_square_jpeg()
    )
    monkeypatch.setattr(
        "src.pipeline.image_sourcing.make_download_client", lambda: client
    )
    # Serve 3 distinct images so the success path is taken.
    for i in (2, 3):
        client.serve(f"https://cdn.example.com/{i}.jpg", _image_bytes(850, 860 + i))
    blobs = await source_product_images(
        _product([f"https://cdn.example.com/{i}.jpg" for i in (1, 2, 3)])
    )
    assert blobs is not None and len(blobs) == 3
    assert TrackingClient.instances[0].closed is True
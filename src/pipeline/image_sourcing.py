"""Deterministic CDN image acquisition from verified supplier products
(plan F.5 — rewritten for the Supplier-First pipeline).

This module has ZERO LLM involvement and zero browser involvement. Images
are sourced EXCLUSIVELY from `RawSupplierProduct.image_urls` — the direct
CDN gallery array the extractor captured from the live supplier listing
itself. Nothing is invented, upgraded by an LLM, or re-scraped from
another site.

Validation gates on every downloaded byte (deterministic, never trusting
HTML width/height attributes): HTTP 200, an allowed image content-type,
decoded shortest side >= 800px, a sane byte-size band, and byte-hash
dedupe so one image served twice counts once toward the minimum.

Hard 3-image minimum: fewer than 3 distinct validated images means the
function returns None and the exporter drops the candidate entirely —
no fallback tier to another site exists.

AliExpress alicdn URLs in a supplier gallery array are size-suffixed
thumbnail variants; `strip_size_suffix` upgrades them to the original
full-resolution asset before downloading (the original is tried first,
the as-served URL kept as fallback ordering).
"""

from __future__ import annotations

import hashlib
import logging
import re
from io import BytesIO
from typing import List, Optional, Set
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image

from src.config import settings
from src.models import RawSupplierProduct

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deterministic validation gates (strict image protocol)
# ---------------------------------------------------------------------------

# Only these response content-types may ever become a product photo.
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}

# Decoded pixel dimensions — never trusted from HTML attributes alone.
MIN_SHORTEST_SIDE = 800

# Hard minimum of distinct validated images per product (plan F.5 export
# gate). Fewer than this -> no export, no exceptions.
MIN_DISTINCT_IMAGES = 3

# Cap on downloaded images per product.
MAX_GALLERY_IMAGES = 8

# Byte-size sanity band: smaller than 15 KB is an icon/avatar/tracking
# pixel; larger than 15 MB is not a product photo.
MIN_IMAGE_BYTES = 15 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024

# Decoded PIL format -> file extension (the exporter's naming convention:
# keep the real format, applied uniformly).
FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

# Marketplace CDN size suffixes on filenames: Shopify `photo_800x800.jpg` /
# `photo_800x.jpg`, AliExpress alicdn `photo.jpg_640x640.jpg` — stripped to
# request the original, largest asset.
_SIZE_SUFFIX_RE = re.compile(r"(_\d+x\d+|_\d+x)$")

# AliExpress alicdn's mid-filename form: the size/quality marker sits
# BETWEEN extensions (`Sxxxx.jpg_960x960q75.jpg_.avif`) so the end-anchored
# regex never matches and the pipeline would ship 220px AVIF thumbnails —
# every one rejected by the >= 800px gate. The original asset is everything
# before the marker.
_ALICDN_SIZE_MARKER_RE = re.compile(r"_\d+x\d+(?:q\d+)?")


def _plausible_image_filename(name: str) -> bool:
    return bool(re.search(r"\.(jpe?g|png|webp)$", name, re.IGNORECASE))


def make_download_client() -> httpx.AsyncClient:
    """Shared download client (browser UA — some CDNs 403 on default httpx UA)."""
    return httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={
            "User-Agent": settings.USER_AGENT,
            "Accept": "image/*,*/*;q=0.8",
        },
    )


def strip_size_suffix(url: str) -> str:
    """Strip a marketplace `_{w}x{h}` size suffix to request the original asset.

    `_800x800` / `_800x` at the end of the filename stem (Shopify CDN), a
    trailing `_640x640` segment (AliExpress alicdn thumbnails), and alicdn's
    mid-filename `photo.jpg_960x960q75.jpg_.avif` form. Crop variants and
    extension-less paths are left as-is.
    """
    parts = urlsplit(url)
    directory, _, filename = parts.path.rpartition("/")
    prefix = f"{directory}/" if directory else ""

    # alicdn mid-filename marker: the original asset is everything before
    # the first `_<w>x<h>[q<q>]` marker, and that prefix must itself end in
    # an image extension (it carries the original's `.jpg`/`.png`).
    marker = _ALICDN_SIZE_MARKER_RE.search(filename)
    if marker and _plausible_image_filename(filename[: marker.start()]):
        stripped = prefix + filename[: marker.start()]
        return urlunsplit((parts.scheme, parts.netloc, stripped, parts.query, ""))

    stem, dot, ext = filename.rpartition(".")
    if not dot:
        # Extension-less paths are left as-is: without an extension we
        # cannot assume the stripped stem is even an image URL.
        return url
    new_stem = _SIZE_SUFFIX_RE.sub("", stem)
    if new_stem == stem:
        return url
    # alicdn thumbnails double up the extension (`photo.jpg_640x640.jpg`):
    # after suffix removal the stem ends in `.ext` again — drop the duplicate
    # so the original (`photo.jpg`) is requested, not `photo.jpg.jpg`.
    if ext and new_stem.lower().endswith(f".{ext.lower()}"):
        new_stem = new_stem[: -(len(ext) + 1)]
    new_filename = f"{new_stem}.{ext}" if ext else new_stem
    new_path = f"{prefix}{new_filename}"
    return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, ""))


def _decode_image(data: bytes) -> Optional[tuple]:
    """Decode downloaded bytes with PIL; return (width, height, format) or None."""
    try:
        with Image.open(BytesIO(data)) as img:
            return img.size[0], img.size[1], (img.format or "").upper()
    except Exception:
        logger.debug("Image bytes could not be decoded", exc_info=True)
        return None


def image_extension(data: bytes) -> str:
    """File extension implied by the blob's actual encoded format."""
    dims = _decode_image(data)
    if dims and dims[2] in FORMAT_EXTENSIONS:
        return FORMAT_EXTENSIONS[dims[2]]
    return ".jpg"  # defensive default; content-type was already checked


async def download_and_validate_image(
    url: str,
    min_shortest_side: int = MIN_SHORTEST_SIDE,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[bytes]:
    """GET one image URL and validate it against every deterministic gate.

    Returns the raw bytes on success, else None (logged at debug). The
    single shared download helper for the supplier CDN source.
    """
    owns_client = client is None
    if client is None:
        client = make_download_client()
    try:
        try:
            response = await client.get(url)
        except Exception:
            logger.debug("Image download failed: %s", url, exc_info=True)
            return None
        if response.status_code != 200:
            logger.debug(
                "Image fetch rejected: %s -> HTTP %d", url, response.status_code
            )
            return None
        content_type = (
            (response.headers.get("content-type") or "")
            .split(";")[0]
            .strip()
            .lower()
        )
        if content_type not in ALLOWED_CONTENT_TYPES:
            logger.debug(
                "Image fetch rejected: %s -> content-type %r", url, content_type
            )
            return None
        data = response.content
        if not (MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES):
            logger.debug(
                "Image fetch rejected: %s -> %d bytes (icon/sprite or oversized)",
                url, len(data),
            )
            return None
        dims = _decode_image(data)
        if dims is None:
            return None
        width, height, _fmt = dims
        if min(width, height) < min_shortest_side:
            logger.debug(
                "Image fetch rejected: %s -> %dx%d (min shortest side %d)",
                url, width, height, min_shortest_side,
            )
            return None
        return data
    finally:
        if owns_client:
            await client.aclose()


async def _collect_valid_images(
    urls: List[str], client: httpx.AsyncClient, max_images: int
) -> List[bytes]:
    """Download candidate URLs in order until `max_images` distinct blobs.

    Dedupes twice: by URL (the same gallery URL counted once) and by
    SHA-256 of the downloaded bytes (the same image served from two URLs —
    thumbnail and full-res — counts once toward the distinct minimum).
    """
    blobs: List[bytes] = []
    seen_urls: Set[str] = set()
    seen_hashes: Set[str] = set()
    for url in urls:
        if len(blobs) >= max_images:
            break
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        data = await download_and_validate_image(url, client=client)
        if not data:
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen_hashes:
            logger.debug("Duplicate image bytes skipped: %s", url)
            continue
        seen_hashes.add(digest)
        blobs.append(data)
    return blobs


# ---------------------------------------------------------------------------
# Public entry point (plan F.5)
# ---------------------------------------------------------------------------


async def source_product_images(
    product: RawSupplierProduct,
    client: Optional[httpx.AsyncClient] = None,
    min_images: int = MIN_DISTINCT_IMAGES,
    max_images: int = MAX_GALLERY_IMAGES,
) -> Optional[List[bytes]]:
    """Download >= `min_images` distinct validated images from the product's
    verified CDN gallery, or None.

    The candidate URLs come exclusively from `RawSupplierProduct.image_urls`
    (the extractor-captured gallery of the live listing). Size-suffixed
    alicdn variants are upgraded to the original asset first, with the
    as-served URL kept as fallback ordering. Hard failure rule: fewer than
    `min_images` distinct validated images -> None, and the exporter drops
    the candidate — no fallback tier exists.
    """
    owns_client = client is None
    if client is None:
        client = make_download_client()
    try:
        # Upgrade suffixed assets: original first, as-served URL as fallback.
        upgraded: List[str] = []
        for url in product.image_urls:
            stripped = strip_size_suffix(url)
            if stripped != url:
                upgraded.append(stripped)
            upgraded.append(url)
        seen = set()
        ordered = [u for u in upgraded if not (u in seen or seen.add(u))][:64]

        blobs = await _collect_valid_images(ordered, client, max_images)
        if len(blobs) < min_images:
            logger.warning(
                "Product gallery yielded only %d distinct valid image(s) "
                "(need %d): supplier=%s url=%s",
                len(blobs), min_images, product.supplier_name,
                product.supplier_retail_url,
            )
            return None
        logger.info(
            "Sourced %d validated image(s) from %s gallery: %s",
            len(blobs), product.supplier_name, product.supplier_retail_url,
        )
        return blobs
    finally:
        if owns_client:
            await client.aclose()
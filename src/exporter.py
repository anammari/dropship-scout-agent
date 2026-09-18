"""Asset downloader & direct workspace exporter (Phase 5, rewritten per F.5).

Takes winning candidates — `(ProductCandidateEvaluation, RawSupplierProduct)`
pairs with an `ACCEPT` verdict — and exports each to the Shopify workspace:

0. Skip the candidate outright when its product page is already present in
   an exported package (`skipped_duplicate`) — re-running the same keyword
   returns the same supplier products, and writing them into fresh
   directories would duplicate the catalog. The check runs first, so a
   duplicate costs no download and creates no directory.
1. `image_sourcing.source_product_images` — download >= 3 distinct
   validated images from the product's verified CDN gallery
   (`RawSupplierProduct.image_urls`). Fewer than 3 -> drop the candidate
   entirely (`dropped_no_valid_images`; no directory is created).
2. Only then create `product-NN/`, write the images, and write
   `metadata.json`. The 3-image minimum is re-asserted defensively
   immediately before the directory is created.

This module never constructs, guesses, or downloads a supplier URL itself
— the URL comes verbatim from the verified `RawSupplierProduct`, and this
module only ever writes bytes `image_sourcing.py` already downloaded and
validated.

    product-NN/
    ├── metadata.json       # exactly the export contract keys
    └── images/
        ├── image-1.jpg
        ├── image-2.jpg
        └── image-3.jpg
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import aiofiles
from httpx import AsyncClient

from src.config import settings
from src.models import ProductCandidateEvaluation, RawSupplierProduct
from src.pipeline.image_sourcing import (
    MIN_DISTINCT_IMAGES,
    image_extension,
    make_download_client,
    source_product_images,
)

logger = logging.getLogger(__name__)

# `product-01`, `product-02`, ... — numbering continues past whatever
# already exists in the export dir so re-runs never overwrite old work.
_PRODUCT_DIR_RE = re.compile(r"^product-(\d{2,})$")

# Images are sourced from the supplier listing's own CDN gallery — kept
# for backward-compatible provenance tracking in metadata.json.
IMAGE_SOURCE = "supplier_gallery"


@dataclass
class ExportResult:
    """Outcome of exporting one accepted candidate."""

    product_dir: str
    product_title: str
    metadata_path: str
    image_files: List[str] = field(default_factory=list)
    image_source: str = IMAGE_SOURCE
    evaluation: Optional[ProductCandidateEvaluation] = None
    supplier_retail_url: str = ""

    @property
    def product_slug(self) -> str:
        return Path(self.product_dir).name


class CandidateExporter:
    """Writes accepted candidates into the Shopify workspace directory."""

    def __init__(
        self,
        export_dir: Optional[str] = None,
        client: Optional[AsyncClient] = None,
    ) -> None:
        self.export_dir = Path(export_dir or settings.EXPORT_DIR)
        self._client = client  # injectable for tests; owned lazily otherwise
        # Per-run funnel counters read by the orchestrator. They accumulate
        # across every `export_candidates` call in the run — the orchestrator
        # may call it once per evaluation round, so resetting per call would
        # silently discard earlier rounds' drops.
        self.dropped_no_valid_images: int = 0
        self.skipped_duplicate: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def export_candidates(
        self,
        candidates: Iterable[
            Tuple[ProductCandidateEvaluation, RawSupplierProduct]
        ],
    ) -> List[ExportResult]:
        """Export every ACCEPT (evaluation, raw product) pair.

        Non-ACCEPT entries are defensive — the orchestrator normally
        pre-filters — and are skipped before any download work or
        directory creation. Candidates failing the 3-image gate are
        dropped entirely (no directory, logged, counted) per F.5's hard
        failure rule.

        Candidates whose product page is already present in an exported
        package are skipped too (`skipped_duplicate`) — re-running the same
        keyword returns the same supplier products, and re-exporting them
        into fresh directories would duplicate the catalog. The check runs
        before any download, so a duplicate costs nothing and creates no
        directory.
        """
        accepted = []
        for evaluation, product in candidates:
            if evaluation.verdict != "ACCEPT":
                logger.warning(
                    "Skipping %r candidate (verdict=%s) at export stage",
                    product.product_title, evaluation.verdict,
                )
                continue
            accepted.append((evaluation, product))

        results: List[ExportResult] = []
        if not accepted:
            return results

        # Already-exported product pages, from disk plus anything this call
        # exports. A URL is only added once its package is actually written,
        # so a candidate dropped at the image gate never blocks a later
        # candidate with the same page.
        exported_urls = self._exported_supplier_urls()

        owns_client = self._client is None
        client = self._client or make_download_client()
        try:
            for evaluation, product in accepted:
                # `evaluation.supplier_retail_url` is the value persisted to
                # metadata.json, so it is the one the dedupe compares.
                if evaluation.supplier_retail_url in exported_urls:
                    self.skipped_duplicate += 1
                    logger.info(
                        "Skipping duplicate candidate (already exported): "
                        "%r -> %s",
                        product.product_title, evaluation.supplier_retail_url,
                    )
                    continue
                try:
                    result = await self._export_candidate(
                        evaluation, product, client
                    )
                except Exception:
                    # One broken product must never abort the export batch.
                    logger.exception(
                        "Export failed for %r (%s); continuing",
                        product.product_title, product.supplier_retail_url,
                    )
                    continue
                if result is not None:
                    results.append(result)
                    exported_urls.add(evaluation.supplier_retail_url)
                    logger.info(
                        "Exported %r -> %s (%d image(s), image_source=%s)",
                        result.product_title, result.product_dir,
                        len(result.image_files), result.image_source,
                    )
        finally:
            if owns_client:
                await client.aclose()
        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _export_candidate(
        self,
        evaluation: ProductCandidateEvaluation,
        product: RawSupplierProduct,
        client: AsyncClient,
    ) -> Optional[ExportResult]:
        """Download the verified gallery; drop on any < 3-images failure."""
        try:
            blobs = await source_product_images(product, client=client)
        except Exception:
            logger.exception(
                "Image sourcing crashed for %r (%s); dropping",
                product.product_title, product.supplier_retail_url,
            )
            blobs = None
        if blobs is None or len(blobs) < MIN_DISTINCT_IMAGES:
            logger.warning(
                "dropped_no_valid_images product_title=%s supplier=%s "
                "url=%s (fewer than %d valid images from the verified CDN "
                "gallery)",
                product.product_title, product.supplier_name,
                product.supplier_retail_url, MIN_DISTINCT_IMAGES,
            )
            self.dropped_no_valid_images += 1
            return None
        return await self._export_one(evaluation, product, blobs)

    def _exported_supplier_urls(self) -> set:
        """Product pages already present in an exported `product-NN` package.

        Reads the `supplier_retail_url` out of every existing package's
        `metadata.json`. A package that cannot be read is ignored with a
        warning rather than aborting the run — policing old exports is not
        this stage's job, and refusing to export anything because one
        directory is malformed would be worse than the duplicate it guards
        against.
        """
        urls: set = set()
        if not self.export_dir.exists():
            return urls
        for entry in self.export_dir.iterdir():
            if not entry.is_dir() or not _PRODUCT_DIR_RE.match(entry.name):
                continue
            metadata_path = entry / "metadata.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning(
                    "Ignoring unreadable package while checking for "
                    "duplicates: %s", metadata_path,
                )
                continue
            if not isinstance(metadata, dict):
                continue
            url = metadata.get("supplier_retail_url")
            if isinstance(url, str) and url:
                urls.add(url)
        return urls

    def _next_product_dir(self) -> Path:
        """First free `product-NN` directory (continues past existing ones)."""
        index = 1
        if self.export_dir.exists():
            for entry in self.export_dir.iterdir():
                match = _PRODUCT_DIR_RE.match(entry.name)
                if entry.is_dir() and match:
                    index = max(index, int(match.group(1)) + 1)
        return self.export_dir / f"product-{index:02d}"

    async def _export_one(
        self,
        evaluation: ProductCandidateEvaluation,
        product: RawSupplierProduct,
        blobs: List[bytes],
    ) -> ExportResult:
        if not product.product_title.strip():
            # Defensive: an ACCEPT with an empty title would produce a
            # junk package — better to drop it than ship it.
            raise ValueError(
                f"ACCEPT with empty product_title ({product.supplier_retail_url})"
            )
        if len(blobs) < MIN_DISTINCT_IMAGES:
            # Defensive gate: should be unreachable (the stage above
            # already enforces it) — treat as a hard internal error,
            # never write a partial package.
            raise ValueError(
                f"Internal error: refusing to export fewer than "
                f"{MIN_DISTINCT_IMAGES} images for {product.supplier_retail_url}"
            )
        product_dir = self._next_product_dir()
        images_dir = product_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        image_files: List[str] = []
        for index, blob in enumerate(blobs, start=1):
            filename = f"image-{index}{image_extension(blob)}"
            async with aiofiles.open(images_dir / filename, "wb") as fh:
                await fh.write(blob)
            image_files.append(filename)

        # Export contract (plan F.5): supplier data verbatim from the
        # verified RawSupplierProduct; marketing fields from the LLM
        # evaluation; margin math recomputed programmatically at
        # evaluation-construction time.
        metadata = {
            "product_title": product.product_title,
            "category": evaluation.niche_category,
            "suggested_price_aud": evaluation.suggested_retail_aud,
            "estimated_cogs_aud": evaluation.estimated_cogs_aud,
            "cogs_estimation_basis": evaluation.cogs_estimation_basis,
            "projected_margin_aud": evaluation.estimated_margin_aud,
            "marketing_ad_copy": evaluation.marketing_ad_copy,
            "features": evaluation.key_features or [evaluation.problem_solved],
            "target_tags": evaluation.target_tags,
            "shipping_notice_au": evaluation.shipping_notice_au,
            "supplier_name": evaluation.supplier_name,
            "supplier_retail_url": evaluation.supplier_retail_url,
            "image_source": IMAGE_SOURCE,
        }
        metadata_path = product_dir / "metadata.json"
        async with aiofiles.open(metadata_path, "w", encoding="utf-8") as fh:
            await fh.write(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")

        return ExportResult(
            product_dir=str(product_dir),
            product_title=product.product_title,
            metadata_path=str(metadata_path),
            image_files=image_files,
            image_source=IMAGE_SOURCE,
            evaluation=evaluation,
            supplier_retail_url=product.supplier_retail_url,
        )
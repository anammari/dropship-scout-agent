"""The shared Google Shopping row shape for the Step-3 gold-product scrape.

`ShoppingRow` is the drop-in row contract the Step-3 scraper
(`google_shopping_hasdata.py`, the pipeline's sole Google Shopping source
since the Apify actor was retired on the Morning Anthem branch) and the
Step-3 curator (`src/evaluators/gold_curator.py`) both read. It used to live
in the Apify actor wrapper; when that wrapper was deleted the dataclass
moved here so the HasData source and the curator keep one shared identity.

Extractor-shaped, deliberately NOT a `BaseSupplierExtractor`: these rows are
Google Shopping marketplace listings (merchant, retail `price_text`,
`rating`/`rating_count`), not supplier dropship listings. There is no
supplier PDP, no freight quote and no gallery to carry, so they can never
become `RawSupplierProduct` without fabricating sourcing fields — the
anti-hallucination contract forbids that. The module is never registered in
`_EXTRACTOR_REGISTRY` / `SUPPLIER_PRIORITY_ORDER`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ShoppingRow:
    """One Google Shopping AU listing, parsed defensively from the API row."""

    title: str
    url: str
    merchant: Optional[str]
    price_text: Optional[str]
    delivery_text: Optional[str]
    image_url: Optional[str]
    description: Optional[str]
    rating: Optional[float]
    rating_count: Optional[int]
    source_keyword: str
    #: The retail price as a NUMBER, so a downstream decision model can reason
    #: over it instead of parsing `price_text`. Taken from the payload's own
    #: numeric `extractedPrice` when the API supplies one, otherwise parsed from
    #: the display string — derived from the row, never invented. None when
    #: neither yields a figure.
    price_value: Optional[float] = None

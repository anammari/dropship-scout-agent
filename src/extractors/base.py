"""Abstract supplier-extractor interface and shared exception types.

Supplier-First pipeline (plan Part F): extractors fetch live, active
supplier products directly — no ad scraping, no landing-page resolution.
Every extractor implements `BaseSupplierExtractor` and returns
`RawSupplierProduct` instances, which downstream stages treat as verified
ground truth.
"""

import abc
from typing import List

from src.models import RawSupplierProduct


class ExtractorBlockedException(Exception):
    """The upstream supplier API/scrape is actively refusing the agent.

    Persistent auth failures, rate-limit walls, or anti-bot challenges —
    the orchestrator should divert to another extractor rather than retry.
    """


class ExtractorTimeoutException(Exception):
    """A request timeout, distinct from a hard block — worth one retry."""


class ExtractorNotConfiguredError(Exception):
    """The extractor's credentials are missing from `.env`."""

    def __init__(self, credential_name: str) -> None:
        super().__init__(
            f"{credential_name} is not configured (set it in .env to enable "
            "this supplier extractor)"
        )
        self.credential_name = credential_name


class BaseSupplierExtractor(abc.ABC):
    """Interface every supplier extractor implements.

    Subclasses override `engine_name` (e.g. "cjdropshipping",
    "aliexpress_apify") so downstream records can trace which engine
    produced them.
    """

    engine_name: str = "base"
    supplier_name: str = "base"

    @abc.abstractmethod
    async def fetch_products(
        self, keywords: List[str], country: str = "AU"
    ) -> List[RawSupplierProduct]:
        """Fetch active supplier listings matching `keywords`.

        Returns only fully-formed `RawSupplierProduct`s (real URL, listed
        price, >= 3 gallery URLs) — anything that cannot satisfy the model
        is skipped upstream.
        """
        raise NotImplementedError
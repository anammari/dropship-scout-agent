"""Step 6 — rank the Step-5 Morning Anthem packages against the gold products.

Intake is the gold-kernel tree written by Step 5
(`<MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/product-NN/`, each a
`metadata.json` + `images/` package — CJdropshipping is The Morning Anthem's
sole supplier) plus the Step-3 gold-standard product list
(`outputs/json/morning-anthem-step-3-gold-products.json`). Every package in
the supplier folder is ranked; nothing is filtered on the way in.

**Ranking writes a report and deletes nothing.** The ranker itself never moves
or deletes a
package directory — it writes a tiered report (shortlist / review / disregard)
to `outputs/json/morning-anthem-step-6-ranked-candidates.json` (+
`outputs/md/…md`) and lets the operator (Step 7) decide what to validate and
link.

Batching (plan §8.2): one System One call carries a single shared state (the
gold reference plus the batch's products) and three questions per product —
`<slug>__similarity` and `<slug>__winning_value` (score) and `<slug>__pillar`
(choice) — for `JEV_BATCH_SIZE` products per call, which is the docs' cited
efficient envelope. A failed call retries once; if it still fails, only that
batch's packages become `disregard` with a note — the run never aborts on one
bad call, and a package whose answer keys are missing lands the same way.

`rank_score = 0.6 * similarity + 0.4 * value` on the 1–5 scale, and the tiers
read `SHORTLIST_MIN_SCORE` / `REVIEW_MIN_SCORE` (plan §8.0/§8.3). Jev's raw
score position is 0-based; `jev_client.answer_score` shifts it onto 1–5 (see
that module's docstring for the live probe finding).
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence

from src.config import settings
from src.keywords.generator import (
    BANNED_TOKENS,
    KeywordGenerationError,
    load_gold_products,
)
from src.ranking.jev_client import (
    QUESTIONS,
    JevClient,
    JevError,
    answer_choice,
    answer_score,
)

logger = logging.getLogger(__name__)

#: The supplier subfolder Step 5 writes (CJdropshipping is the sole supplier).
SUPPLIER_DIRS = ("cjdropshipping",)

#: A package directory is `product-NN` (two or more digits), same shape the
#: exporter allocates and the duplicate scan matches.
_PRODUCT_DIR_RE = re.compile(r"^product-(\d{2,})$")

#: Composite rank weights (plan §8.3). Kept as named constants so the one
#: place judgement lives can be read at a glance.
SIMILARITY_WEIGHT = 0.6
VALUE_WEIGHT = 0.4

#: The question names each product carries, in the order they are assembled.
QUESTION_NAMES = ("similarity", "winning_value", "pillar")

#: Step-3 fields carried into the gold reference of every call's state.
GOLD_REFERENCE_FIELDS = (
    "name",
    "description",
    "retail_price_text",
    "demand_evidence",
)

#: Package `metadata.json` fields projected into a call's per product state, so
#: the model reads the AUD figures as structured data rather than parsing prose.
#: `markup_multiplier` is NOT one of them: the export contract has no such key,
#: and it is derived in code (below) because the model must not do the division —
#: the repo's rule is that LLM arithmetic is never trusted.
#: A supplier photo larger than this is skipped rather than inlined; the
#: gallery JPEGs observed run 30-60 KB, so only a pathological entry trips it.
JEV_MAX_IMAGE_BYTES = 4_000_000

PRODUCT_STATE_FIELDS = (
    "product_title",
    "category",
    "suggested_price_aud",
    "estimated_cogs_aud",
    "projected_margin_aud",
    "features",
    "target_tags",
    "shipping_notice_au",
    "supplier_name",
    "supplier_retail_url",
)


def _markup_multiplier(metadata: Dict[str, Any]) -> Optional[float]:
    """Suggested retail ÷ landed cost, to two places, or None.

    Supplied because the margin floor is an OR — 2.5x markup OR AUD 10 gross —
    and a model judging "winning value" should see both legs without dividing. A
    zero or missing cost yields None rather than a division error or an infinite
    markup.
    """
    retail = metadata.get("suggested_price_aud")
    cogs = metadata.get("estimated_cogs_aud")
    for value in (retail, cogs):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    if cogs <= 0:
        return None
    return round(retail / cogs, 2)


def _first_image_data_url(package_dir: Path) -> Optional[str]:
    """The package's first gallery image as a data URL, or None.

    The images are already on disk from Step 5, so this costs no fetch and no
    HasData credit — it only stops the decider from judging an object it has
    never seen. A missing or unreadable gallery degrades to text-only rather
    than failing the ranking.
    """
    if not settings.JEV_SEND_IMAGES or settings.JEV_IMAGES_PER_PACKAGE < 1:
        return None
    try:
        images = sorted((Path(package_dir) / "images").glob("image-*"))
    except OSError:
        return None
    for image in images[: settings.JEV_IMAGES_PER_PACKAGE]:
        try:
            raw = image.read_bytes()
        except OSError:
            continue
        if not raw or len(raw) > JEV_MAX_IMAGE_BYTES:
            continue
        mime = mimetypes.guess_type(image.name)[0] or "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    return None


def _product_state(
    metadata: Dict[str, Any], image_url: Optional[str] = None
) -> Dict[str, Any]:
    """The per-product state entry: the projected fields plus the derived markup.

    `image_url` is the package's own supplier photo as a data URL (operator
    directive 2026-10-16). The decider accepts images in `state`, and the photo
    is the only field that settles what the object actually IS — a supplier
    title reading `Hand-made Coffee Pot Set` sat on a photo of a porcelain drip
    cone, which no text field reveals.
    """
    state = {field: metadata.get(field) for field in PRODUCT_STATE_FIELDS}
    state["markup_multiplier"] = _markup_multiplier(metadata)
    if image_url:
        state["product_image"] = image_url
    return state


#: The post-evaluation compliance gate: the AICIS boundary tokens — one source
#: of truth with the keyword engine — plus the battery terms. A package whose
#: human text matches one lands `disregard` with an explicit note, regardless of
#: its score: contraband never reaches shortlist/review.
#:
#: **Electrical goods were REMOVED from this list on 2026-10-16** (operator
#: directive). The store sells to Australia but is not an AU-registered
#: electrical retailer, so mains and USB goods are no longer auto-deleted —
#: they are ESCALATED instead (`ELECTRICAL_ESCALATION_TOKENS` below), and the
#: operator checks compliance with the Electrical Equipment Safety System and
#: ACMA standards against CJ before linking anything.
#:
#: **Batteries stay banned outright**, and they are a different case from a
#: mains appliance: a cell is a shipping hazard regardless of certification.
BATTERY_TOKENS = (
    "battery",
    "batteries",
    "li-ion",
    "lithium",
    "aa battery",
    "aaa battery",
)

COMPLIANCE_BANNED_TOKENS = tuple(BANNED_TOKENS) + BATTERY_TOKENS

#: Terms that flag a package for a MANUAL electrical-safety check rather than
#: deleting it. A shortlisted match keeps its tier and gains an `[ESCALATE]`
#: note naming the standard, so the operator sees it in the report and on the
#: row rather than discovering it after a live listing.
ELECTRICAL_ESCALATION_TOKENS = (
    "electric",
    "electrical",
    "usb",
    "rechargeable",
    "cordless",
    "mains",
    "adapter",
    "led",
)

#: Fields whose text is SUPPLIER FACT — copied from the CJ listing. A banned
#: token here describes what the product actually is, so the match is decisive.
COMPLIANCE_SUPPLIER_FIELDS = ("product_title",)

#: Fields the PIPELINE WROTE — the LLM's marketing payload and feature list.
#: A banned token here is a word choice, not a product fact: the ad copy for a
#: wooden board mentioned "oil" (board conditioning) and the gate deleted the
#: highest-scoring package of the run. An advisory match must not delete.
COMPLIANCE_LLM_FIELDS = ("marketing_ad_copy", "features")

#: The human-text fields the gate scans. `metadata.json` carries no
#: description field, so the description role is the marketing payload.
COMPLIANCE_SCAN_FIELDS = COMPLIANCE_SUPPLIER_FIELDS + COMPLIANCE_LLM_FIELDS


@dataclass(frozen=True)
class ComplianceHit:
    """One banned-token match, and whether it can delete the package.

    `decisive` is True only for a supplier-fact field: that match forces
    `disregard` regardless of score. An advisory (LLM-authored) match keeps
    its note but leaves the tier to the score, because the pipeline must not
    delete a product over wording the pipeline itself chose.
    """

    note: str
    decisive: bool


def _scan_compliance_fields(metadata: Dict[str, Any], fields: Sequence[str]) -> str:
    """The `compliance gate: ...` note for the first match in `fields`, else ''.

    Deterministic: fields in the order given, a field's strings left to right,
    tokens in tuple order — the same case-insensitive substring rule every
    other banned-token check in the repo uses, so the note names exactly where
    the token sits.
    """
    for field in fields:
        value = metadata.get(field)
        texts = value if isinstance(value, list) else (value,)
        for text in texts:
            lowered = str(text or "").lower()
            for token in COMPLIANCE_BANNED_TOKENS:
                if token in lowered:
                    return (
                        f"compliance gate: banned token {token!r} matched "
                        f"in {field}"
                    )
    return ""


def _electrical_hit(metadata: Dict[str, Any]) -> Optional[str]:
    """The `[ESCALATE]` note for an electrical product, or None.

    Not a compliance failure: mains and USB goods are sellable, they simply
    need the operator to confirm the supplier's certification against the
    Electrical Equipment Safety System and ACMA standards before a listing goes
    live. Scanning the same three human-text fields keeps the two checks
    consistent; supplier fields are scanned first so the note names the
    strongest evidence.
    """
    for field in COMPLIANCE_SCAN_FIELDS:
        value = metadata.get(field)
        texts = value if isinstance(value, list) else (value,)
        for text in texts:
            lowered = str(text or "").lower()
            for token in ELECTRICAL_ESCALATION_TOKENS:
                if token in lowered:
                    return (
                        f"[ESCALATE] electrical ({token!r} in {field}) — confirm "
                        "EESS/ACMA compliance with CJ before linking"
                    )
    return None


def _compliance_hit(metadata: Dict[str, Any]) -> Optional[ComplianceHit]:
    """The compliance verdict for a package, or None when its text is clean.

    Supplier fields are scanned first so a decisive match always outranks an
    advisory one — a package whose title says "electric" is contraband even
    if its ad copy is innocent.
    """
    supplier_note = _scan_compliance_fields(metadata, COMPLIANCE_SUPPLIER_FIELDS)
    if supplier_note:
        return ComplianceHit(supplier_note, decisive=True)
    llm_note = _scan_compliance_fields(metadata, COMPLIANCE_LLM_FIELDS)
    if llm_note:
        return ComplianceHit(llm_note, decisive=False)
    return None


class JevRankingError(JevError):
    """The intake is missing or unusable — there is nothing to rank."""


@dataclass
class PackageRef:
    """One `product-NN` package found on disk, before ranking."""

    package_dir: str  # absolute path to the package directory
    slug: str  # state key, engine-qualified and question-id safe
    metadata: Dict[str, Any]  # the package's metadata.json, verbatim


@dataclass
class RankedPackage:
    """One package's Jev verdict and derived tier."""

    package_dir: str
    metadata: Dict[str, Any]
    similarity_score: Optional[float]  # 1..5 level position
    value_score: Optional[float]  # 1..5 level position
    pillar: Optional[str]  # Jev choice, or None when the call gave nothing
    rank_score: float  # 0.6*similarity + 0.4*value
    tier: Literal["shortlist", "review", "disregard"]
    notes: str = ""


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _slug(engine: str, dir_name: str) -> str:
    """A question-id-safe, engine-qualified key for a package.

    Question ids are `<slug>__<question>`, so the slug must not itself contain
    `__`; and the engine qualifies the slug so packages from independently
    numbered supplier folders can never collide in one state.
    """
    safe = re.sub(r"[^a-z0-9_]", "_", f"{engine}_{dir_name}".lower())
    return safe


class JevProductRanker:
    """Ranks every gold-kernel package against the gold-standard products."""

    def __init__(
        self,
        client: Optional[JevClient] = None,
        gold_products_path: Optional[Path] = None,
        export_root: Optional[Path] = None,
        batch_size: Optional[int] = None,
        shortlist_min_score: Optional[float] = None,
        review_min_score: Optional[float] = None,
    ) -> None:
        self.gold_products_path = Path(
            gold_products_path or settings.MORNING_ANTHEM_GOLD_PRODUCTS_PATH
        )
        self.export_root = Path(
            export_root or settings.MORNING_ANTHEM_EXPORT_DIR
        )
        self.batch_size = (
            batch_size if batch_size is not None else settings.JEV_BATCH_SIZE
        )
        self.shortlist_min_score = (
            shortlist_min_score
            if shortlist_min_score is not None
            else settings.JEV_SHORTLIST_MIN_SCORE
        )
        self.review_min_score = (
            review_min_score
            if review_min_score is not None
            else settings.JEV_REVIEW_MIN_SCORE
        )
        if self.batch_size < 1:
            raise JevRankingError(f"batch_size must be >= 1 (got {self.batch_size})")
        # The gold set is settled first so a missing Step-3 deliverable fails
        # before any (billable) System One call.
        self.gold_products = self._load_gold()
        # The client last: with an injected client this never touches `.env`,
        # so tests need no credential.
        self.client = client if client is not None else JevClient()

    # --- intake -----------------------------------------------------------

    def _load_gold(self) -> List[Dict[str, Any]]:
        try:
            return load_gold_products(self.gold_products_path)
        except KeywordGenerationError as exc:
            raise JevRankingError(str(exc)) from exc

    def collect_packages(self) -> List[PackageRef]:
        """Every `product-NN` package under the supplier folder(s).

        A package whose `metadata.json` is missing or unreadable is skipped
        with a warning rather than aborting the run — the same tolerance the
        exporter's duplicate scan applies, and for the same reason: one
        malformed directory must not void a whole ranking. Nothing found at
        all is a hard failure naming the Step-5 runner.
        """
        packages: List[PackageRef] = []
        for engine in SUPPLIER_DIRS:
            supplier_dir = self.export_root / engine
            if not supplier_dir.exists():
                continue
            for entry in sorted(supplier_dir.iterdir()):
                if not entry.is_dir() or not _PRODUCT_DIR_RE.match(entry.name):
                    continue
                metadata_path = entry / "metadata.json"
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    logger.warning(
                        "Skipping unreadable package while collecting Step-5 "
                        "output: %s", metadata_path,
                    )
                    continue
                if not isinstance(metadata, dict):
                    logger.warning(
                        "Skipping package with non-object metadata: %s",
                        metadata_path,
                    )
                    continue
                packages.append(
                    PackageRef(
                        package_dir=str(entry),
                        slug=_slug(engine, entry.name),
                        metadata=metadata,
                    )
                )
        if not packages:
            raise JevRankingError(
                f"No Step-5 packages found under {self.export_root} "
                f"(expected {{{','.join(SUPPLIER_DIRS)}}}/product-NN/). Run "
                "Step 5 first: source .venv/bin/activate && python "
                "scripts/ingest_keyword_bank.py"
            )
        return packages

    # --- ranking ----------------------------------------------------------

    def rank(self) -> List[RankedPackage]:
        """Rank every collected package; returns them by descending score."""
        packages = self.collect_packages()
        gold_reference = self._gold_reference()
        logger.info(
            "Ranking %d package(s) against %d gold-standard product(s) in "
            "batches of %d",
            len(packages), len(gold_reference), self.batch_size,
        )
        ranked: List[RankedPackage] = []
        for batch in _chunks(packages, self.batch_size):
            ranked.extend(self._rank_batch(list(batch), gold_reference))
        return sorted(ranked, key=lambda p: (-p.rank_score, p.package_dir))

    def _gold_reference(self) -> List[Dict[str, Any]]:
        return [
            {key: product.get(key) for key in GOLD_REFERENCE_FIELDS}
            for product in self.gold_products
        ]

    def _rank_batch(
        self,
        batch: List[PackageRef],
        gold_reference: List[Dict[str, Any]],
    ) -> List[RankedPackage]:
        state = {
            "gold_reference": gold_reference,
            # Structured data, not prose: the AUD figures and the derived markup
            # ride here so the model reads them rather than parsing a sentence.
            "products": {
                ref.slug: _product_state(
                    ref.metadata, _first_image_data_url(ref.package_dir)
                )
                for ref in batch
            },
        }
        questions: Dict[str, Dict[str, Any]] = {}
        for ref in batch:
            for name in QUESTION_NAMES:
                template = QUESTIONS[name]
                questions[f"{ref.slug}__{name}"] = {
                    **template,
                    "instructions": template["instructions"].format(slug=ref.slug),
                }
        try:
            answers = self._decide_with_retry(state, questions)
        except JevError as exc:
            logger.warning(
                "System One batch of %d package(s) failed after a retry: %s",
                len(batch), exc,
            )
            note = f"Jev batch call failed: {exc}"
            return [self._disregarded(ref, note) for ref in batch]
        return [self._read_answer(ref, answers) for ref in batch]

    def _decide_with_retry(
        self, state: Dict[str, Any], questions: Dict[str, Dict[str, Any]]
    ) -> Dict[str, dict]:
        """One System One call, retried once on failure (plan §8.3)."""
        try:
            return self.client.decide(state, questions)
        except JevError:
            logger.warning("System One call failed; retrying once")
            return self.client.decide(state, questions)

    def _read_answer(
        self, ref: PackageRef, answers: Dict[str, dict]
    ) -> RankedPackage:
        similarity = answer_score(answers.get(f"{ref.slug}__similarity"))
        value = answer_score(answers.get(f"{ref.slug}__winning_value"))
        pillar = answer_choice(answers.get(f"{ref.slug}__pillar"))

        if similarity is None or value is None:
            notes = [
                "Jev returned no "
                + ("similarity" if similarity is None else "winning-value")
                + " score"
            ]
            if pillar == "discard":
                notes.append("Jev classified this package as discard")
            compliance = _compliance_hit(ref.metadata)
            if compliance:
                notes.append(compliance.note)
            return RankedPackage(
                package_dir=ref.package_dir,
                metadata=ref.metadata,
                similarity_score=similarity,
                value_score=value,
                pillar=pillar,
                rank_score=0.0,
                tier="disregard",
                notes="; ".join(notes),
            )

        rank_score = SIMILARITY_WEIGHT * similarity + VALUE_WEIGHT * value
        tier: Literal["shortlist", "review", "disregard"]
        if rank_score >= self.shortlist_min_score:
            tier = "shortlist"
        elif rank_score >= self.review_min_score:
            tier = "review"
        else:
            tier = "disregard"

        notes = ""
        if pillar == "discard":
            notes = "Jev classified this package as discard"
        compliance = _compliance_hit(ref.metadata)
        if compliance:
            # A SUPPLIER-field match is an override, not a suggestion: the
            # product IS the contraband, so it never reaches shortlist/review
            # regardless of its Jev score. An LLM-authored match only records
            # the note — deleting over the pipeline's own word choice would
            # void a good product, which is what happened to the top-scoring
            # package of the 2026-10-13 run.
            if notes:
                notes += "; "
            notes += compliance.note
            if compliance.decisive and tier != "disregard":
                logger.warning(
                    "Compliance gate demoted %s from %s to disregard: %s",
                    ref.slug, tier, compliance.note,
                )
                tier = "disregard"
            elif not compliance.decisive:
                logger.info(
                    "Compliance gate advisory on %s (tier left at %s): %s",
                    ref.slug, tier, compliance.note,
                )

        if tier in ("shortlist", "review"):
            escalation = _electrical_hit(ref.metadata)
            if escalation:
                # Kept, not deleted — but flagged so an uncertified mains or
                # USB good cannot reach a live listing unnoticed.
                if notes:
                    notes += "; "
                notes += escalation
                logger.warning(
                    "Electrical escalation on %s (%s): %s",
                    ref.slug, tier, escalation,
                )
        logger.info(
            "Ranked %s: similarity=%.2f value=%.2f rank=%.2f tier=%s pillar=%s",
            ref.slug, similarity, value, rank_score, tier, pillar,
        )
        return RankedPackage(
            package_dir=ref.package_dir,
            metadata=ref.metadata,
            similarity_score=similarity,
            value_score=value,
            pillar=pillar,
            rank_score=rank_score,
            tier=tier,
            notes=notes,
        )

    def _disregarded(self, ref: PackageRef, note: str) -> RankedPackage:
        return RankedPackage(
            package_dir=ref.package_dir,
            metadata=ref.metadata,
            similarity_score=None,
            value_score=None,
            pillar=None,
            rank_score=0.0,
            tier="disregard",
            notes=note,
        )


# --- report writing (a report, never a deletion — plan §8.3) ---------------

_TIER_ORDER = ("shortlist", "review", "disregard")


def packages_to_payload(
    packages: List[RankedPackage],
    *,
    gold_products_path: Path,
    export_root: Path,
    batch_size: int,
    shortlist_min_score: float,
    review_min_score: float,
) -> Dict[str, Any]:
    """The structured JSON ranking document."""
    tiers = {tier: [] for tier in _TIER_ORDER}
    for package in packages:
        tiers[package.tier].append(package.package_dir)
    return {
        "summary": {
            "packages_ranked": len(packages),
            "tier_counts": {tier: len(dirs) for tier, dirs in tiers.items()},
            "batch_size": batch_size,
            "similarity_weight": SIMILARITY_WEIGHT,
            "value_weight": VALUE_WEIGHT,
            "shortlist_min_score": shortlist_min_score,
            "review_min_score": review_min_score,
            "gold_products_path": str(gold_products_path),
            "export_root": str(export_root),
            "model": settings.JEV_MODEL,
        },
        "packages": [asdict(package) for package in packages],
        "tiers": tiers,
    }


def _fmt_score(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.2f}"


def render_markdown(packages: List[RankedPackage], payload: Dict[str, Any]) -> str:
    """The tier-grouped Markdown digest of the ranking."""
    summary = payload["summary"]
    counts = summary["tier_counts"]
    lines = [
        "# Step 6 — Jev ranked candidates",
        "",
        f"{summary['packages_ranked']} package(s) ranked against the Step-3 "
        f"gold-standard products ({summary['gold_products_path']}).",
        "",
        f"Model `{summary['model']}`; rank score is "
        f"{summary['similarity_weight']:.1f}·similarity + "
        f"{summary['value_weight']:.1f}·value on a 1–5 scale. Tiers: "
        f"shortlist ≥ {summary['shortlist_min_score']:.1f}, "
        f"review ≥ {summary['review_min_score']:.1f}, else disregard.",
        "",
        f"Shortlist: {counts['shortlist']} · Review: {counts['review']} · "
        f"Disregard: {counts['disregard']}",
        "",
    ]
    for tier in _TIER_ORDER:
        members = [p for p in packages if p.tier == tier]
        lines.append(f"## {tier.capitalize()} ({len(members)})")
        lines.append("")
        if not members:
            lines.append("_None._")
            lines.append("")
            continue
        lines.append(
            "| package | title | supplier | similarity | value | rank | pillar |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for package in members:
            title = str(package.metadata.get("product_title") or "").replace("|", "\\|")
            supplier = str(package.metadata.get("supplier_name") or "")
            lines.append(
                f"| {package.package_dir} | {title} | {supplier} | "
                f"{_fmt_score(package.similarity_score)} | "
                f"{_fmt_score(package.value_score)} | "
                f"{package.rank_score:.2f} | {package.pillar or '—'} |"
            )
        lines.append("")
        noted = [p for p in members if p.notes]
        if noted:
            lines.append("Notes:")
            lines.append("")
            for package in noted:
                lines.append(f"- `{package.package_dir}`: {package.notes}")
            lines.append("")
    return "\n".join(lines)


def write_reports(
    packages: List[RankedPackage],
    json_path: Path,
    md_path: Path,
    *,
    gold_products_path: Path,
    export_root: Path,
    batch_size: int,
    shortlist_min_score: float,
    review_min_score: float,
) -> Dict[str, Any]:
    """Write the JSON ranking and its Markdown digest; returns the payload."""
    payload = packages_to_payload(
        packages,
        gold_products_path=gold_products_path,
        export_root=export_root,
        batch_size=batch_size,
        shortlist_min_score=shortlist_min_score,
        review_min_score=review_min_score,
    )
    json_path = Path(json_path)
    md_path = Path(md_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(packages, payload), encoding="utf-8")
    return payload

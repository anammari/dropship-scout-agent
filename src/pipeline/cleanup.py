"""Step 7 — clean the gold-kernel tree from the Step-6 ranking report.

**This is the only step that deletes anything, and it is irreversible.**

The Step-6 ranker writes a tiered report and moves nothing. The operator's
override (2026-10-12) is to keep the workspace pristine, so this step acts on
that report:

* **`shortlist`** — kept, every package.
* **`review`** — the top `CLEANUP_REVIEW_KEEP` by `rank_score` are kept, the rest
  are deleted.
* **`disregard`** — deleted, every package.

Why it is arranged the way it is:

* **Planning and acting are separate.** `plan_cleanup` decides and returns what
  it decided; `apply_cleanup` does the deleting. The runner calls the first
  always and the second only when asked, so a dry run is not a special mode — it
  is simply the plan without the act.
* **Deletion costs money to undo.** A package holds images downloaded from the
  supplier's CDN and a metadata.json assembled from a paid HasData scrape, CJ
  round trips and LLM calls. Deleting one wrongly is not a `git checkout` away,
  which is why the tier floors should be reviewed on a live run before the first
  destructive pass.
* **Containment over trust.** The report is data, and a path in it could be
  anything. Every candidate is resolved and checked to sit strictly inside the
  supplier folder before it can be touched; anything else is refused and the step
  deletes nothing at all.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.config import settings

logger = logging.getLogger(__name__)

#: A package directory is `product-NN`, the shape the exporter allocates.
_PRODUCT_DIR_RE = re.compile(r"^product-\d{2,}$")

#: The tiers the report uses, and what each one means here.
_DELETE_TIERS = ("disregard",)

#: Product families that must not flood the catalogue (operator directive
#: 2026-10-16). The 2026-10-13 run kept 23 `sunrise_move` packages of which 22
#: were dog gear — a pet-accessories shop wearing a morning-lifestyle name.
#:
#: A package's family is derived from its TITLE, not from an upstream field:
#: deterministic, free, and it therefore applies to packages already on disk
#: without a re-ingestion. Only the families listed here are capped; a package
#: matching none is unconstrained.
FAMILY_RULES: Dict[str, tuple] = {
    "dog-gear": ("dog", "puppy", "leash", "harness", "collar", "poop", "waste bag"),
}


class CleanupError(Exception):
    """The ranking report is missing, malformed, or unsafe to act on."""


def package_family(title: object) -> Optional[str]:
    """The capped family a package title belongs to, or None.

    Case-insensitive substring match, the same rule every other text check in
    the repo uses. `FAMILY_RULES` order decides when a title matches two
    families; there is one family today, so that is a non-question until a
    second is added.
    """
    lowered = str(title or "").lower()
    for family, tokens in FAMILY_RULES.items():
        if any(token in lowered for token in tokens):
            return family
    return None


@dataclass
class CleanupAction:
    """One package and what the cleanup decided about it."""

    package_dir: Path
    tier: str
    rank_score: Optional[float]
    #: True when this package is to be removed.
    delete: bool
    reason: str


@dataclass
class CleanupReport:
    """What `apply_cleanup` actually did."""

    deleted: List[Path] = field(default_factory=list)
    already_absent: List[Path] = field(default_factory=list)
    failed: List[tuple] = field(default_factory=list)  # (path, error message)

    @property
    def kept(self) -> int:
        """Reported by the runner from the plan; here for symmetry."""
        return 0


def _supplier_root(export_root: Optional[Path] = None) -> Path:
    """The one directory this step may ever delete inside."""
    root = Path(export_root or settings.MORNING_ANTHEM_EXPORT_DIR)
    return root / "cjdropshipping"


def _is_contained(package_dir: Path, supplier_root: Path) -> bool:
    """Whether `package_dir` is a `product-NN` directory strictly inside the root.

    Both sides are resolved first, so a symlinked package — or a report naming
    `../../something` — resolves outside the root and is refused. The root
    itself, a file, and a directory that is not named `product-NN` are all
    refused too: this step deletes product packages and nothing else.
    """
    try:
        resolved = package_dir.resolve()
        root = supplier_root.resolve()
    except OSError:
        return False
    if not _PRODUCT_DIR_RE.match(resolved.name):
        return False
    if resolved == root:
        return False
    # A path that EXISTS but is not a directory is refused; a path that is
    # simply gone is still safely contained, so a re-run reports it as already
    # absent rather than as a refusal.
    if resolved.exists() and not resolved.is_dir():
        return False
    return root in resolved.parents


def load_ranked_packages(report_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read the Step-6 report's packages (fail closed, with remediation)."""
    path = Path(report_path or settings.MORNING_ANTHEM_RANKED_CANDIDATES_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CleanupError(
            f"ranking report not found at {path} ({exc}); run Step 6 first: "
            "source .venv/bin/activate && python scripts/rank_optimal_candidates.py"
        ) from exc
    except ValueError as exc:
        raise CleanupError(
            f"ranking report at {path} is not valid JSON ({exc}); re-run "
            "Step 6 rather than hand-editing it"
        ) from exc
    packages = payload.get("packages") if isinstance(payload, dict) else None
    if not isinstance(packages, list):
        raise CleanupError(
            f"ranking report at {path} carries no 'packages' list; re-run "
            "Step 6 (scripts/rank_optimal_candidates.py)"
        )
    rows = [row for row in packages if isinstance(row, dict)]
    if not rows:
        raise CleanupError(
            f"ranking report at {path} carries no packages — there is nothing "
            "for the cleanup to act on"
        )
    return rows


def plan_cleanup(
    packages: Sequence[Dict[str, Any]],
    export_root: Optional[Path] = None,
    review_keep: Optional[int] = None,
    max_per_family: Optional[int] = None,
) -> List[CleanupAction]:
    """Decide what to delete, without deleting anything.

    Shortlist packages are all kept. Review packages are ordered by score
    (descending, ties broken by directory so the cut is reproducible) and only
    the top `review_keep` survive. Disregard packages go. A row whose tier is
    none of the three — or whose path is not safely contained — is **kept** and
    reported as such: this step deletes only what it positively recognises, so a
    future report shape can never turn into a mass deletion.

    **The family cap runs last and can override a keep** (`max_per_family`,
    operator directive 2026-10-16). It is GLOBAL, not per pillar: the strongest
    `max_per_family` survivors of a capped family anywhere in the tree stay,
    and the rest go — including `shortlist` ones, because the whole point is
    that no single product family may flood the catalogue however well it
    scores. Zero or None disables it.
    """
    supplier_root = _supplier_root(export_root)
    keep = settings.CLEANUP_REVIEW_KEEP if review_keep is None else review_keep
    if keep < 0:
        raise CleanupError(f"review_keep must be >= 0 (got {keep})")
    family_cap = (
        settings.CLEANUP_MAX_PER_FAMILY
        if max_per_family is None
        else max_per_family
    )
    if family_cap < 0:
        raise CleanupError(f"max_per_family must be >= 0 (got {family_cap})")

    actions: List[CleanupAction] = []
    review: List[CleanupAction] = []

    for row in packages:
        raw_dir = str(row.get("package_dir") or "").strip()
        tier = str(row.get("tier") or "")
        score = row.get("rank_score")
        score = float(score) if isinstance(score, (int, float)) else None
        package_dir = Path(raw_dir) if raw_dir else Path("<none>")

        if not raw_dir or not _is_contained(package_dir, supplier_root):
            actions.append(
                CleanupAction(
                    package_dir=package_dir,
                    tier=tier,
                    rank_score=score,
                    delete=False,
                    reason=(
                        "not a product-NN directory inside "
                        f"{supplier_root} — refused and left alone"
                    ),
                )
            )
            continue

        if tier == "shortlist":
            actions.append(
                CleanupAction(package_dir, tier, score, False, "shortlist — kept")
            )
        elif tier == "disregard":
            actions.append(
                CleanupAction(package_dir, tier, score, True, "disregard — deleted")
            )
        elif tier == "review":
            review.append(CleanupAction(package_dir, tier, score, False, ""))
        else:
            actions.append(
                CleanupAction(
                    package_dir,
                    tier,
                    score,
                    False,
                    f"unknown tier {tier!r} — kept (cleanup deletes only what it "
                    "recognises)",
                )
            )

    # The review cap: the strongest survive, ties broken by path so the same
    # report always yields the same cut.
    review.sort(
        key=lambda action: (-(action.rank_score or 0.0), str(action.package_dir))
    )
    for position, action in enumerate(review, start=1):
        if position <= keep:
            action.delete = False
            action.reason = f"review #{position} of {len(review)} — kept (cap {keep})"
        else:
            action.delete = True
            action.reason = (
                f"review #{position} of {len(review)} — deleted (beyond the "
                f"cap of {keep})"
            )
    actions.extend(review)

    _apply_family_cap(packages, actions, family_cap)

    return actions


def _apply_family_cap(
    packages: Sequence[Dict[str, Any]],
    actions: List[CleanupAction],
    family_cap: int,
) -> None:
    """Delete every capped-family survivor beyond the strongest `family_cap`.

    Runs after the tier rules and overrides them, so a `shortlist` package can
    be cut here — that is the point of the rule. Ordering is by score then
    path, the same reproducible tie-break the review cap uses, so the same
    report always yields the same cut.
    """
    if family_cap <= 0:
        return
    titles = {
        str(row.get("package_dir") or "").strip(): (
            (row.get("metadata") or {}).get("product_title")
            if isinstance(row.get("metadata"), dict)
            else None
        )
        for row in packages
    }
    by_family: Dict[str, List[CleanupAction]] = {}
    for action in actions:
        if action.delete:
            continue
        family = package_family(titles.get(str(action.package_dir), ""))
        if family:
            by_family.setdefault(family, []).append(action)

    for family, survivors in by_family.items():
        if len(survivors) <= family_cap:
            continue
        survivors.sort(
            key=lambda action: (-(action.rank_score or 0.0), str(action.package_dir))
        )
        for position, action in enumerate(survivors, start=1):
            if position <= family_cap:
                action.reason += f"; {family} #{position} of {len(survivors)} — kept (cap {family_cap})"
            else:
                action.delete = True
                action.reason = (
                    f"{family} #{position} of {len(survivors)} — deleted "
                    f"(beyond the {family} cap of {family_cap}; was {action.tier})"
                )
        logger.info(
            "Family cap %s: %d survivor(s), keeping the strongest %d",
            family, len(survivors), family_cap,
        )


def apply_cleanup(
    actions: Iterable[CleanupAction], export_root: Optional[Path] = None
) -> CleanupReport:
    """Delete the packages the plan marked. Re-checks containment on the way in.

    The containment check runs **again here**, not only in the plan: the two
    functions are separate entry points, so `apply_cleanup` must be safe even
    when a caller hands it hand-built actions. A package that is already gone is
    reported as absent rather than raising — a re-run should be a no-op, not a
    failure.
    """
    supplier_root = _supplier_root(export_root)
    report = CleanupReport()
    for action in actions:
        if not action.delete:
            continue
        if not _is_contained(action.package_dir, supplier_root):
            # Should be unreachable through `plan_cleanup`; here so that a
            # hand-built action cannot escape the supplier folder either.
            report.failed.append(
                (action.package_dir, f"refused: not inside {supplier_root}")
            )
            logger.error(
                "cleanup refused to delete %s: not a product-NN directory "
                "inside %s", action.package_dir, supplier_root,
            )
            continue
        try:
            shutil.rmtree(action.package_dir)
        except FileNotFoundError:
            report.already_absent.append(action.package_dir)
            logger.info("cleanup: %s was already absent", action.package_dir)
            continue
        except OSError as exc:
            report.failed.append((action.package_dir, str(exc)))
            logger.error(
                "cleanup failed to delete %s: %s", action.package_dir, exc
            )
            continue
        report.deleted.append(action.package_dir)
        logger.info("cleanup deleted %s (%s)", action.package_dir, action.reason)
    return report


def summarize(actions: Sequence[CleanupAction]) -> Dict[str, int]:
    """Counts for the runner's summary line."""
    counts: Dict[str, int] = {
        "kept": 0,
        "deleted": 0,
        "refused": 0,
    }
    for action in actions:
        if action.delete:
            counts["deleted"] += 1
        elif "refused" in action.reason or "unknown tier" in action.reason:
            counts["refused"] += 1
        else:
            counts["kept"] += 1
    return counts

"""Offline unit tests for Step 6 — the Jev product ranker (plan §8.5).

No network: the System One endpoint is a scripted `httpx.MockTransport` when
the transport itself is under test, and a recording fake client everywhere
else; the Step-3 gold deliverable and the Step-5 gold-kernel tree are
`tmp_path` fixtures, so no real `outputs/` or workspace path is read.

Covers the System One envelope (top-level `answers`) and its four failure
modes, the typed answer readers (including the +1 shift that puts Jev's
0-based score position onto the 1–5 scale the tier thresholds read), the
question builders, package collection (both supplier folders, independent
numbering, unreadable metadata skipped, empty tree is a remediation error),
batch assembly (one shared state, three questions per package, engine-
qualified slugs), the composite score and its tier boundaries, the retry-once
rule, and the tiered report writer.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest

from src.config import settings
from src.ranking.jev_client import (
    JEV_SCORE_INDEX_SHIFT,
    PILLAR_OPTIONS,
    QUESTIONS,
    REVIEW_MIN_SCORE,
    SHORTLIST_MIN_SCORE,
    SIMILARITY_LEVELS,
    VALUE_LEVELS,
    JevClient,
    JevConfigError,
    JevError,
    answer_choice,
    answer_noul,
    answer_score,
)
from src.keywords.generator import BANNED_TOKENS
from src.ranking.jev_product_ranker import (
    COMPLIANCE_BANNED_TOKENS,
    GOLD_REFERENCE_FIELDS,
    PRODUCT_STATE_FIELDS,
    SIMILARITY_WEIGHT,
    VALUE_WEIGHT,
    JevProductRanker,
    JevRankingError,
    RankedPackage,
    _compliance_hit,
    packages_to_payload,
    render_markdown,
    write_reports,
)

GOLD_PRODUCTS = [
    {
        "name": "Stainless garlic press",
        "pillar": "curated_home",
        "demand_evidence": "rating 4.7/5, 1,204 reviews (Google Shopping AU)",
        "description": "Zinc-alloy press with a silicone garlic peeler.",
        "retail_price_text": "$24.95",
    },
    {
        "name": "Bamboo bath caddy",
        "pillar": "self_care_rituals",
        "demand_evidence": "rating 4.8/5, 2,310 reviews (Google Shopping AU)",
        "description": None,
        "retail_price_text": "$39.95",
    },
]

METADATA = {
    "product_title": "Garlic rocker press",
    "category": "Kitchen tools",
    "suggested_price_aud": 34.95,
    "estimated_cogs_aud": 11.4,
    "cogs_estimation_basis": "Supplier listed price AUD 3.86 plus freight.",
    "projected_margin_aud": 23.55,
    "marketing_ad_copy": "Crush garlic in one rock.",
    "features": ["One-hand rocking action", "Stainless head"],
    "target_tags": ["dropship", "kitchen"],
    "shipping_notice_au": "Ships to Australia from the supplier.",
    "supplier_name": "CJdropshipping",
    "supplier_retail_url": "https://cjdropshipping.com/product/123.html",
    "image_source": "supplier_gallery",
}


# --- fixtures -------------------------------------------------------------


def _write_gold(tmp_path, products=None, name="gold.json"):
    """Write a Step-3-shaped deliverable and return its path."""
    path = tmp_path / name
    path.write_text(
        json.dumps({"products": GOLD_PRODUCTS if products is None else products}),
        encoding="utf-8",
    )
    return path


def _write_package(root, engine, dir_name, metadata=None, raw=None):
    """Write one `product-NN` package and return its directory."""
    package_dir = root / engine / dir_name
    package_dir.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else json.dumps(metadata or METADATA)
    (package_dir / "metadata.json").write_text(text, encoding="utf-8")
    return package_dir


def _export_root(tmp_path, packages=(("cjdropshipping", "product-04"),)):
    """A gold-kernel tree with the given (engine, dir) packages."""
    root = tmp_path / "optimal-dropship-candidates"
    for engine, dir_name in packages:
        _write_package(root, engine, dir_name)
    return root


class ScriptedJevClient:
    """A recording fake: one scripted answers map per call, in order."""

    def __init__(self, script=None):
        self.calls = []
        self._script = script or (lambda state, questions, index: {})

    def decide(self, state, questions):
        index = len(self.calls)
        self.calls.append({"state": state, "questions": questions})
        return self._script(state, questions, index)

    def question_ids(self, call=0):
        return list(self.calls[call]["questions"])


def _perfect_answers(questions, similarity=4, value=4, pillar="curated_home"):
    """Answer every question in `questions` with the given RAW score levels."""
    answers = {}
    for qid in questions:
        if qid.endswith("__similarity"):
            answers[qid] = {"type": "score", "score": similarity}
        elif qid.endswith("__winning_value"):
            answers[qid] = {"type": "score", "score": value}
        elif qid.endswith("__pillar"):
            answers[qid] = {"type": "choice", "choice": pillar}
    return answers


def _ranker(tmp_path, client, **kwargs):
    kwargs.setdefault("gold_products_path", _write_gold(tmp_path))
    if "export_root" not in kwargs:
        kwargs["export_root"] = _export_root(tmp_path)
    return JevProductRanker(client=client, **kwargs)


# --- the System One transport (httpx.MockTransport) -----------------------


def test_decide_posts_the_documented_envelope():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"answers": {"a": {"noul": 0.9}}})

    client = JevClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://openrouter.test/api/v1",
        api_key="mock-test-openrouter_api_key",
        model="typesafe/jev-1.13",
    )
    answers = client.decide({"products": {}}, {"a": client.noul("Is it a tool?")})

    assert seen["url"] == "https://openrouter.test/api/v1/systemone"
    assert seen["auth"] == "Bearer mock-test-openrouter_api_key"
    assert seen["body"]["model"] == "typesafe/jev-1.13"
    assert seen["body"]["state"] == {"products": {}}
    assert seen["body"]["questions"]["a"] == {
        "type": "noul",
        "instructions": "Is it a tool?",
    }
    # The answers map is returned unwrapped -- `answers` sits at top level.
    assert answers == {"a": {"noul": 0.9}}


def test_decide_base_url_trailing_slash_is_trimmed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://jev.test/systemone"
        return httpx.Response(200, json={"answers": {}})

    client = JevClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="http://jev.test/",
        model="typesafe/jev-1.13",
    )
    assert client.decide({}, {}) == {}


def test_decide_http_error_raises():
    transport = httpx.MockTransport(lambda request: httpx.Response(402, text="no credit"))
    client = JevClient(
        client=httpx.Client(transport=transport),
        base_url="http://jev.test",
        model="typesafe/jev-1.13",
    )
    with pytest.raises(JevError, match="System One HTTP 402"):
        client.decide({}, {})


def test_decide_transport_error_raises():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = JevClient(
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
        base_url="http://jev.test",
        model="typesafe/jev-1.13",
    )
    with pytest.raises(JevError, match="System One transport error"):
        client.decide({}, {})


def test_decide_non_json_and_missing_answers_raise():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))
    client = JevClient(
        client=httpx.Client(transport=transport),
        base_url="http://jev.test",
        model="typesafe/jev-1.13",
    )
    with pytest.raises(JevError, match="non-JSON body"):
        client.decide({}, {})

    # A 200 that parses but carries no `answers` object is the one seam the
    # vendor could move the payload behind -- it must fail loudly, not return
    # an empty ranking.
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"result": {"answers": {}}})
    )
    client = JevClient(
        client=httpx.Client(transport=transport),
        base_url="http://jev.test",
        model="typesafe/jev-1.13",
    )
    with pytest.raises(JevError, match="no `answers` object"):
        client.decide({}, {})


def test_missing_config_raises(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", None)
    with pytest.raises(JevConfigError) as excinfo:
        JevClient()
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_question_builders_shape():
    client = JevClient(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        model="typesafe/jev-1.13",
    )
    assert client.score("rate it", ["a", "b"]) == {
        "type": "score",
        "instructions": "rate it",
        "criteria": ["a", "b"],
    }
    assert client.choice("pick", {"x": "one"}) == {
        "type": "choice",
        "instructions": "pick",
        "criteria": {"x": "one"},
    }
    assert client.noul("yes?") == {"type": "noul", "instructions": "yes?"}
    assert client.noul("yes?", {"true": "it is"})["criteria"] == {"true": "it is"}


# --- answer readers (the 0-based score finding) ----------------------------


def test_answer_score_shifts_zero_based_positions_onto_the_one_to_five_scale():
    # The probe returned score 3.24 for a 5-entry criteria list whose legend
    # keys ran "0".."4" -- a 0-based position. The shift makes that 4.24.
    assert JEV_SCORE_INDEX_SHIFT == 1.0
    assert answer_score({"score": 3.24}) == pytest.approx(4.24)
    assert answer_score({"score": 0}) == 1.0
    assert answer_score({"score": 4}) == 5.0


def test_answer_readers_reject_unusable_answers():
    assert answer_score(None) is None
    assert answer_score({}) is None
    assert answer_score({"score": "3"}) is None
    assert answer_score({"score": True}) is None
    assert answer_choice({"choice": ""}) is None
    assert answer_choice({"choice": 7}) is None
    assert answer_noul({"noul": None}) is None
    assert answer_noul({"noul": False}) is None

    assert answer_choice({"choice": "curated_home"}) == "curated_home"
    assert answer_noul({"noul": 0.99}) == pytest.approx(0.99)


def test_question_constants_are_the_judgement_surface():
    assert len(SIMILARITY_LEVELS) == 5
    assert len(VALUE_LEVELS) == 5
    assert set(PILLAR_OPTIONS) == {
        "curated_home",
        "self_care_rituals",
        "other",
        "discard",
    }
    assert set(QUESTIONS) == {"similarity", "winning_value", "pillar"}
    for name, template in QUESTIONS.items():
        # Every instruction names the product by the shared state's path, and
        # the slug is the one slot the ranker fills.
        assert "{slug}" in template["instructions"]
        template["instructions"].format(slug="cjdropshipping_product_04")
    assert QUESTIONS["similarity"]["criteria"] == SIMILARITY_LEVELS
    assert QUESTIONS["winning_value"]["criteria"] == VALUE_LEVELS
    assert QUESTIONS["pillar"]["criteria"] is PILLAR_OPTIONS


def test_thresholds_are_positive_and_ordered():
    assert 0 < REVIEW_MIN_SCORE < SHORTLIST_MIN_SCORE
    assert (SHORTLIST_MIN_SCORE, REVIEW_MIN_SCORE) == (
        settings.JEV_SHORTLIST_MIN_SCORE,
        settings.JEV_REVIEW_MIN_SCORE,
    )


# --- intake ---------------------------------------------------------------


def test_collect_packages_spans_both_suppliers_and_qualifies_slugs(tmp_path):
    root = _export_root(
        tmp_path,
        packages=(
            ("cjdropshipping", "product-04"),
            ("cjdropshipping", "product-25"),
            ("aliexpress", "product-04"),
        ),
    )
    ranker = _ranker(tmp_path, ScriptedJevClient(), export_root=root)
    packages = ranker.collect_packages()

    # The same physical folder name in two suppliers is two packages, and the
    # slugs stay distinct -- question ids are `<slug>__<question>`, so a
    # collision would silently overwrite one supplier's answers.
    slugs = sorted(p.slug for p in packages)
    assert slugs == [
        "aliexpress_product_04",
        "cjdropshipping_product_04",
        "cjdropshipping_product_25",
    ]
    assert all("__" not in p.slug for p in packages)
    assert all(json.loads(Path(p.package_dir, "metadata.json").read_text()) for p in packages)


def test_collect_packages_skips_non_packages_and_unreadable_metadata(tmp_path, caplog):
    root = _export_root(tmp_path, packages=(("cjdropshipping", "product-04"),))
    # Decoys the scan must ignore: a non product-named dir, a stray file, and
    # a package whose metadata.json is not valid JSON.
    (root / "cjdropshipping" / "notes").mkdir()
    (root / "cjdropshipping" / "README.md").write_text("hi", encoding="utf-8")
    _write_package(root, "aliexpress", "product-03", raw="{not json")
    # Non-object JSON is equally unusable.
    _write_package(root, "aliexpress", "product-09", raw="[1, 2, 3]")

    ranker = _ranker(tmp_path, ScriptedJevClient(), export_root=root)
    with caplog.at_level(logging.WARNING):
        packages = ranker.collect_packages()

    assert [p.slug for p in packages] == ["cjdropshipping_product_04"]
    assert sum("Skipping" in r.message for r in caplog.records) == 2


def test_collect_packages_empty_tree_is_a_remediation_error(tmp_path):
    root = tmp_path / "optimal-dropship-candidates"
    root.mkdir()
    ranker = _ranker(tmp_path, ScriptedJevClient(), export_root=root)
    with pytest.raises(JevRankingError) as excinfo:
        ranker.collect_packages()
    assert "ingest_keyword_bank.py" in str(excinfo.value)


def test_missing_gold_deliverable_fails_before_any_call(tmp_path):
    with pytest.raises(JevRankingError) as excinfo:
        JevProductRanker(
            client=ScriptedJevClient(),
            gold_products_path=tmp_path / "absent.json",
            export_root=_export_root(tmp_path),
        )
    assert "run Step 3 first" in str(excinfo.value)


def test_batch_size_below_one_is_rejected(tmp_path):
    with pytest.raises(JevRankingError, match="batch_size"):
        _ranker(tmp_path, ScriptedJevClient(), batch_size=0)


# --- batch assembly -------------------------------------------------------


def test_rank_batches_packages_and_shares_one_state(tmp_path):
    root = _export_root(
        tmp_path,
        packages=tuple(
            ("cjdropshipping", f"product-{n:02d}") for n in range(4, 9)
        ),
    )
    client = ScriptedJevClient(lambda state, questions, i: _perfect_answers(questions))
    ranker = _ranker(tmp_path, client, export_root=root, batch_size=2)

    ranked = ranker.rank()

    assert len(ranked) == 5
    # 5 packages at 2 per call -> 3 calls (2 + 2 + 1).
    assert len(client.calls) == 3
    assert [len(c["questions"]) for c in client.calls] == [6, 6, 3]

    first = client.calls[0]
    # One shared state per call, carrying the whole gold reference and only
    # the batch's own products.
    assert set(first["state"]) == {"gold_reference", "products"}
    assert len(first["state"]["gold_reference"]) == len(GOLD_PRODUCTS)
    assert set(first["state"]["gold_reference"][0]) == set(GOLD_REFERENCE_FIELDS)
    assert sorted(first["state"]["products"]) == [
        "cjdropshipping_product_04",
        "cjdropshipping_product_05",
    ]
    # The per-product state carries the contract fields, and nothing else.
    assert set(first["state"]["products"]["cjdropshipping_product_04"]) == set(
        PRODUCT_STATE_FIELDS
    )
    assert set(client.question_ids(0)) == {
        "cjdropshipping_product_04__similarity",
        "cjdropshipping_product_04__winning_value",
        "cjdropshipping_product_04__pillar",
        "cjdropshipping_product_05__similarity",
        "cjdropshipping_product_05__winning_value",
        "cjdropshipping_product_05__pillar",
    }


def test_rank_scores_and_tier_boundaries(tmp_path):
    # Seven packages, one per boundary case, all in a single batch:
    #   raw 4 / raw 4 -> 5.0 / 5.0 -> rank 5.0      shortlist
    #   raw 3 / raw 3 -> 4.0 / 4.0 -> rank 4.0      shortlist
    #   raw 2.5 / raw 2.5 -> 3.5 -> rank 3.5        shortlist (exactly at 3.5)
    #   raw 2.4 / raw 2.4 -> 3.4 -> rank 3.4        review (one tick under)
    #   raw 1.5 / raw 1.5 -> 2.5   -> rank 2.5      review (exactly at 2.5)
    #   raw 1.4 / raw 1.4 -> 2.4   -> rank 2.4      disregard
    #   raw 0 / raw 0 -> 1.0 / 1.0 -> rank 1.0      disregard
    levels = {
        "product-04": (4, 4),
        "product-05": (3, 3),
        "product-09": (2.5, 2.5),
        "product-10": (2.4, 2.4),
        "product-06": (1.5, 1.5),
        "product-07": (1.4, 1.4),
        "product-08": (0, 0),
    }
    root = _export_root(
        tmp_path, packages=tuple(("cjdropshipping", d) for d in levels)
    )
    by_slug = {
        f"cjdropshipping_{dir_name.replace('-', '_')}": pair
        for dir_name, pair in levels.items()
    }

    def answers_for(state, questions, index):
        answers = {}
        for qid in questions:
            slug, question = qid.rsplit("__", 1)
            similarity, value = by_slug[slug]
            if question == "similarity":
                answers[qid] = {"type": "score", "score": similarity}
            elif question == "winning_value":
                answers[qid] = {"type": "score", "score": value}
            else:
                answers[qid] = {"type": "choice", "choice": "curated_home"}
        return answers

    client = ScriptedJevClient(answers_for)
    ranker = _ranker(tmp_path, client, export_root=root, batch_size=8)

    by_dir = {p.package_dir.rsplit("/", 1)[-1]: p for p in ranker.rank()}

    assert by_dir["product-04"].tier == "shortlist"
    assert by_dir["product-04"].rank_score == pytest.approx(5.0)
    assert by_dir["product-05"].tier == "shortlist"
    assert by_dir["product-05"].rank_score == pytest.approx(4.0)
    assert by_dir["product-09"].tier == "shortlist"
    assert by_dir["product-09"].rank_score == pytest.approx(3.5)
    assert by_dir["product-10"].tier == "review"
    assert by_dir["product-10"].rank_score == pytest.approx(3.4)
    assert by_dir["product-06"].tier == "review"
    assert by_dir["product-06"].rank_score == pytest.approx(2.5)
    assert by_dir["product-07"].tier == "disregard"
    assert by_dir["product-07"].rank_score == pytest.approx(2.4)
    assert by_dir["product-08"].tier == "disregard"
    # The 0-based -> 1-5 shift is visible in the reported scores.
    assert by_dir["product-04"].similarity_score == pytest.approx(5.0)
    assert by_dir["product-08"].value_score == pytest.approx(1.0)


def test_rank_is_sorted_by_descending_rank_score(tmp_path):
    root = _export_root(
        tmp_path,
        packages=(("cjdropshipping", "product-04"), ("cjdropshipping", "product-05")),
    )
    levels = {
        "cjdropshipping_product_04": (0, 0),
        "cjdropshipping_product_05": (4, 4),
    }
    client = ScriptedJevClient(
        lambda state, questions, i: {
            qid: {"type": "score", "score": levels[qid.rsplit("__", 1)[0]][0]}
            for qid in questions
            if qid.endswith(("__similarity", "__winning_value"))
        }
    )
    ranker = _ranker(tmp_path, client, export_root=root, batch_size=8)
    ranked = ranker.rank()
    assert [p.rank_score for p in ranked] == sorted(
        (p.rank_score for p in ranked), reverse=True
    )
    assert ranked[0].rank_score > ranked[-1].rank_score


def test_composite_weights_are_the_configured_split():
    assert SIMILARITY_WEIGHT == 0.6
    assert VALUE_WEIGHT == 0.4


# --- the post-evaluation compliance gate -----------------------------------


def test_compliance_tokens_are_the_union_specified():
    # The AICIS boundary tokens are the keyword engine's own tuple (one
    # source of truth); the electrical/logistics terms ride on top.
    for token in ("electric", "usb", "rechargeable", "jade", "quartz", "salt"):
        assert token in COMPLIANCE_BANNED_TOKENS
    assert all(token in COMPLIANCE_BANNED_TOKENS for token in BANNED_TOKENS)


def test_compliance_hit_names_the_field_and_the_token():
    jade = {**METADATA, "product_title": "Green Jade Roller Stone Face Tool"}
    assert "matched in product_title" in _compliance_hit(jade)
    assert "'jade'" in _compliance_hit(jade)
    usb = {
        **METADATA,
        "marketing_ad_copy": "Charge it once with any cable — USB ready.",
    }
    assert "'usb'" in _compliance_hit(usb)
    assert "matched in marketing_ad_copy" in _compliance_hit(usb)
    rechar = {**METADATA, "features": ["Rechargeable motor", "Soft head"]}
    assert "'rechargeable'" in _compliance_hit(rechar)
    assert "matched in features" in _compliance_hit(rechar)
    # The clean reference package has no hit on any scanned field.
    assert _compliance_hit(METADATA) == ""


def test_a_shortlisted_package_matching_a_token_is_demoted_to_disregard(tmp_path):
    """A perfect Jev score cannot rescue a contraband package.

    The gate is a deterministic override AFTER the score: title carrying
    `jade` (a shortlisted-scoring stone tool) → tier disregard with the
    explicit reason in notes, exactly as directed.
    """
    root = _export_root(tmp_path, packages=(("cjdropshipping", "product-04"),))
    _write_package(root, "cjdropshipping", "product-04", metadata={
        **METADATA,
        "product_title": "Jade Roller Face Massager Stone Tool",
    })
    client = ScriptedJevClient(
        lambda state, questions, i: _perfect_answers(questions)
    )
    ranker = _ranker(tmp_path, client, export_root=root)
    (package,) = ranker.rank()

    assert package.tier == "disregard"
    assert package.rank_score == pytest.approx(5.0)  # the score is recorded
    assert "compliance gate: banned token 'jade' matched in product_title" in package.notes


def test_a_review_package_with_a_usb_copy_is_demoted_too(tmp_path):
    root = _export_root(tmp_path, packages=(("cjdropshipping", "product-05"),))
    _write_package(root, "cjdropshipping", "product-05", metadata={
        **METADATA,
        "marketing_ad_copy": "Recharge quickly — one USB cable included.",
    })
    client = ScriptedJevClient(
        lambda state, questions, i: _perfect_answers(
            questions, similarity=2, value=2, pillar="curated_home"
        )
    )
    ranker = _ranker(tmp_path, client, export_root=root)
    (package,) = ranker.rank()

    # raw 2 -> 3.0 both questions -> rank 3.0: review, then overridden.
    assert package.tier == "disregard"
    assert "banned token 'usb' matched in marketing_ad_copy" in package.notes


def test_clean_packages_keep_their_tier_and_a_discard_note_composes(tmp_path):
    root = _export_root(
        tmp_path, packages=(("cjdropshipping", "product-06"), ("cjdropshipping", "product-07"))
    )
    _write_package(root, "cjdropshipping", "product-07", metadata={
        **METADATA,
        "product_title": "Stainless Steel Gua Sha Board",
        "marketing_ad_copy": "A stone-free scraping board.",
    })
    def answers(state, questions, i):
        # Per-question, since one call carries both products' questions.
        out = {}
        for qid in questions:
            slug, name = qid.rsplit("__", 1)
            if name == "pillar":
                choice = "discard" if slug.endswith("product_06") else "curated_home"
                out[qid] = {"type": "choice", "choice": choice}
            else:
                out[qid] = {"type": "score", "score": 4}
        return out
    client = ScriptedJevClient(answers)
    ranker = _ranker(tmp_path, client, export_root=root, batch_size=8)
    ranked = {p.package_dir.rsplit("/", 1)[-1]: p for p in ranker.rank()}

    assert ranked["product-06"].tier == "shortlist"
    assert ranked["product-06"].notes == "Jev classified this package as discard"
    assert ranked["product-07"].tier == "shortlist"
    assert ranked["product-07"].notes == ""


# --- failure handling -----------------------------------------------------


def test_missing_answer_keys_land_as_disregard_with_a_note(tmp_path):
    root = _export_root(tmp_path, packages=(("cjdropshipping", "product-04"),))
    # A call that answers only the similarity question: the value score is
    # absent, so the package cannot be tiered on a real composite.
    client = ScriptedJevClient(
        lambda state, questions, i: {
            "cjdropshipping_product_04__similarity": {"type": "score", "score": 4},
            "cjdropshipping_product_04__pillar": {
                "type": "choice",
                "choice": "curated_home",
            },
        }
    )
    ranker = _ranker(tmp_path, client, export_root=root)
    (package,) = ranker.rank()

    assert package.tier == "disregard"
    assert package.rank_score == 0.0
    assert package.similarity_score == pytest.approx(5.0)
    assert package.value_score is None
    assert "no winning-value score" in package.notes


def test_discard_pillar_is_recorded_as_a_note_without_changing_the_tier(tmp_path):
    root = _export_root(tmp_path, packages=(("cjdropshipping", "product-04"),))
    client = ScriptedJevClient(
        lambda state, questions, i: _perfect_answers(
            questions, similarity=4, value=4, pillar="discard"
        )
    )
    ranker = _ranker(tmp_path, client, export_root=root)
    (package,) = ranker.rank()

    assert package.tier == "shortlist"
    assert package.pillar == "discard"
    assert "discard" in package.notes


def test_a_failed_batch_retries_once_then_disregards_only_that_batch(tmp_path):
    root = _export_root(
        tmp_path,
        packages=(("cjdropshipping", "product-04"), ("cjdropshipping", "product-05")),
    )

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def decide(self, state, questions):
            self.calls += 1
            raise JevError("System One HTTP 502: upstream")

    client = FlakyClient()
    ranker = _ranker(tmp_path, client, export_root=root, batch_size=2)
    ranked = ranker.rank()

    # One batch, tried twice, and the run still completes.
    assert client.calls == 2
    assert len(ranked) == 2
    for package in ranked:
        assert package.tier == "disregard"
        assert package.rank_score == 0.0
        assert "Jev batch call failed" in package.notes
        assert "502" in package.notes


def test_a_batch_that_succeeds_on_retry_is_ranked_normally(tmp_path):
    root = _export_root(tmp_path, packages=(("cjdropshipping", "product-04"),))

    class OnceFlaky:
        def __init__(self):
            self.calls = 0

        def decide(self, state, questions):
            self.calls += 1
            if self.calls == 1:
                raise JevError("System One transport error: read timeout")
            return _perfect_answers(questions)

    client = OnceFlaky()
    ranker = _ranker(tmp_path, client, export_root=root)
    (package,) = ranker.rank()

    assert client.calls == 2
    assert package.tier == "shortlist"
    assert package.notes == ""


def test_a_second_batch_still_runs_after_the_first_exhausts_its_retry(tmp_path):
    root = _export_root(
        tmp_path,
        packages=(
            ("cjdropshipping", "product-04"),
            ("cjdropshipping", "product-05"),
            ("cjdropshipping", "product-06"),
        ),
    )

    class FirstBatchDead:
        def __init__(self):
            self.calls = 0

        def decide(self, state, questions):
            self.calls += 1
            if self.calls <= 2:  # first batch, both attempts
                raise JevError("System One HTTP 500: boom")
            return _perfect_answers(questions)

    client = FirstBatchDead()
    ranker = _ranker(tmp_path, client, export_root=root, batch_size=1)
    ranked = ranker.rank()

    assert client.calls == 4  # 2 failed attempts + 2 good calls
    by_dir = {p.package_dir.rsplit("/", 1)[-1]: p for p in ranked}
    assert by_dir["product-04"].tier == "disregard"
    assert by_dir["product-05"].tier == "shortlist"
    assert by_dir["product-06"].tier == "shortlist"


# --- the report -----------------------------------------------------------


def _ranked_samples():
    return [
        RankedPackage(
            package_dir="/root/cjdropshipping/product-04",
            metadata=METADATA,
            similarity_score=5.0,
            value_score=4.5,
            pillar="curated_home",
            rank_score=4.8,
            tier="shortlist",
        ),
        RankedPackage(
            package_dir="/root/aliexpress/product-03",
            metadata={**METADATA, "product_title": "Bath caddy", "supplier_name": "AliExpress"},
            similarity_score=3.0,
            value_score=2.5,
            pillar="self_care_rituals",
            rank_score=2.8,
            tier="review",
        ),
        RankedPackage(
            package_dir="/root/aliexpress/product-09",
            metadata={**METADATA, "product_title": "Broken"},
            similarity_score=None,
            value_score=None,
            pillar=None,
            rank_score=0.0,
            tier="disregard",
            notes="Jev batch call failed: System One HTTP 502",
        ),
    ]


def test_payload_groups_the_tiers_and_counts_them(tmp_path):
    payload = packages_to_payload(
        _ranked_samples(),
        gold_products_path=tmp_path / "gold.json",
        export_root=tmp_path,
        batch_size=4,
        shortlist_min_score=4.0,
        review_min_score=2.5,
    )
    assert payload["summary"]["packages_ranked"] == 3
    assert payload["summary"]["tier_counts"] == {
        "shortlist": 1,
        "review": 1,
        "disregard": 1,
    }
    assert payload["tiers"]["shortlist"] == ["/root/cjdropshipping/product-04"]
    assert payload["tiers"]["disregard"] == ["/root/aliexpress/product-09"]
    assert payload["summary"]["similarity_weight"] == SIMILARITY_WEIGHT
    assert payload["summary"]["value_weight"] == VALUE_WEIGHT
    assert len(payload["packages"]) == 3
    # Every ranked package carries its metadata verbatim for the reviewer.
    assert payload["packages"][0]["metadata"]["supplier_retail_url"] == (
        METADATA["supplier_retail_url"]
    )


def test_markdown_is_tier_grouped_and_shows_an_em_dash_for_missing_scores(tmp_path):
    packages = _ranked_samples()
    payload = packages_to_payload(
        packages,
        gold_products_path=tmp_path / "gold.json",
        export_root=tmp_path,
        batch_size=4,
        shortlist_min_score=4.0,
        review_min_score=2.5,
    )
    markdown = render_markdown(packages, payload)

    assert "## Shortlist (1)" in markdown
    assert "## Review (1)" in markdown
    assert "## Disregard (1)" in markdown
    assert "Garlic rocker press" in markdown
    assert "| — | — |" in markdown  # the unparsable answer, not a crash
    assert "Jev batch call failed" in markdown


def test_markdown_renders_empty_tiers(tmp_path):
    packages = [p for p in _ranked_samples() if p.tier == "shortlist"]
    payload = packages_to_payload(
        packages,
        gold_products_path=tmp_path / "gold.json",
        export_root=tmp_path,
        batch_size=4,
        shortlist_min_score=4.0,
        review_min_score=2.5,
    )
    markdown = render_markdown(packages, payload)
    assert "## Review (0)" in markdown
    assert "_None._" in markdown


def test_write_reports_writes_both_files(tmp_path):
    packages = _ranked_samples()
    json_path = tmp_path / "outputs" / "step-6-ranked-candidates.json"
    md_path = tmp_path / "outputs" / "step-6-ranked-candidates.md"

    payload = write_reports(
        packages,
        json_path,
        md_path,
        gold_products_path=tmp_path / "gold.json",
        export_root=tmp_path,
        batch_size=4,
        shortlist_min_score=4.0,
        review_min_score=2.5,
    )

    assert json_path.exists() and md_path.exists()
    # The parent directory is created on demand -- `outputs/` is gitignored
    # and may not exist on a fresh checkout.
    reloaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert reloaded == payload
    assert md_path.read_text(encoding="utf-8").startswith(
        "# Step 6 — Jev ranked candidates"
    )

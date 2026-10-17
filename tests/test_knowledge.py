"""Knowledge store: typed memory, recall scoping, promotion, doc dedupe.

Every test writes into a temp directory. Nothing here touches the repo, the
network or a model.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from swarmkit.knowledge import (
    DEFAULT_TTL_SECONDS,
    MAX_DOC_CHARS,
    MAX_SEARCH_TEXT_CHARS,
    MAX_VALUE_CHARS,
    Doc,
    Fact,
    Hypothesis,
    Knowledge,
    KnowledgeError,
    State,
    content_hash,
    normalise_url,
    redact_private,
    redact_secrets,
    relevance,
)

TRACKED = "https://docs.example.com/guide/"


class TempStore(unittest.TestCase):
    """One isolated store per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "knowledge"
        self.store = Knowledge(self.root, ttl_seconds=3600.0)

    def raw(self, name: str) -> str:
        path = self.root / name
        return path.read_text(encoding="utf-8") if path.exists() else ""


class TestUrlNormalisation(unittest.TestCase):
    def test_tracking_params_and_fragment_are_stripped(self) -> None:
        key = normalise_url("https://Docs.Example.com/Guide/?utm_source=x&utm_medium=y#section")
        self.assertEqual(key, "https://docs.example.com/Guide")

    def test_meaningful_query_params_survive_and_are_ordered(self) -> None:
        a = normalise_url("https://example.com/s?b=2&a=1&utm_campaign=z")
        b = normalise_url("https://example.com/s?a=1&b=2")
        self.assertEqual(a, b)

    def test_host_is_lowercased_and_trailing_slash_collapsed(self) -> None:
        self.assertEqual(
            normalise_url("https://EXAMPLE.com/a/b/"),
            normalise_url("https://example.com/a/b"),
        )

    def test_default_port_is_dropped(self) -> None:
        self.assertEqual(normalise_url("https://example.com:443/x"), "https://example.com/x")

    def test_credentials_never_reach_the_key(self) -> None:
        key = normalise_url("https://user:s3cr3t@example.com/private")
        self.assertNotIn("s3cr3t", key)
        self.assertEqual(key, "https://example.com/private")

    def test_doubled_path_slashes_are_collapsed(self) -> None:
        self.assertEqual(
            normalise_url("https://example.com//a//b///"),
            normalise_url("https://example.com/a/b"),
        )


class TestDocDedupe(TempStore):
    def test_same_url_with_tracking_params_and_fragment_is_one_entry(self) -> None:
        self.store.cache_doc(
            "https://example.com/guide/?utm_source=newsletter&utm_medium=email#intro",
            "the guide",
            "2026-01-01T00:00:00Z",
        )
        result = self.store.cache_doc(
            "https://example.com/guide/",
            "the guide",
            "2026-01-02T00:00:00Z",
        )
        self.assertEqual(result["reason"], "unchanged")
        self.assertEqual(len(self.store.docs()), 1)
        self.assertEqual(self.store.docs()[0].url, "https://example.com/guide")

    def test_different_pages_are_separate_entries(self) -> None:
        self.store.cache_doc("https://example.com/a", "alpha", "2026-01-01T00:00:00Z")
        self.store.cache_doc("https://example.com/b", "beta", "2026-01-01T00:00:00Z")
        self.assertEqual(len(self.store.docs()), 2)

    def test_identical_content_under_two_urls_becomes_an_alias(self) -> None:
        first = self.store.cache_doc(
            "https://example.com/a", "same bytes", "2026-01-01T00:00:00Z")
        second = self.store.cache_doc(
            "https://example.com/b", "same bytes", "2026-01-01T00:00:00Z")
        self.assertEqual(second["reason"], "alias")
        self.assertEqual(len(self.store.docs()), 1)
        self.assertEqual(second["doc"].url, first["doc"].url)
        self.assertIn("https://example.com/b", second["doc"].aliases)
        # ...and the alias still resolves.
        self.assertTrue(self.store.lookup("https://example.com/b")["hit"])

    def test_changed_content_updates_in_place(self) -> None:
        self.store.cache_doc("https://example.com/a", "v1", "2026-01-01T00:00:00Z")
        result = self.store.cache_doc("https://example.com/a", "v2", "2026-01-02T00:00:00Z")
        self.assertEqual(result["reason"], "updated")
        self.assertTrue(result["changed"])
        docs = self.store.docs()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].content, "v2")
        self.assertEqual(docs[0].revisions, 2)


class TestNoChurn(TempStore):
    def test_refetching_unchanged_content_writes_nothing(self) -> None:
        self.store.cache_doc("https://example.com/a", "stable body", "2026-01-01T00:00:00Z")
        before = self.raw("docs.jsonl")
        stamp_before = self.store.docs_path.stat().st_mtime_ns

        result = self.store.cache_doc(
            "https://example.com/a",
            "stable body",
            "2026-01-05T00:00:00Z",
            etag="W/123",
        )
        self.assertEqual(result["reason"], "unchanged")
        self.assertFalse(result["stored"])
        self.assertFalse(result["changed"])
        self.assertEqual(len(self.store.docs()), 1)
        self.assertEqual(self.raw("docs.jsonl"), before)
        self.assertEqual(self.store.docs_path.stat().st_mtime_ns, stamp_before)

    def test_unchanged_refetch_does_not_reset_freshness(self) -> None:
        """A poll must not keep an expired page looking fresh forever."""
        self.store.cache_doc("https://example.com/a", "body", "2020-01-01T00:00:00Z")
        later = "2026-01-01T00:00:00Z"
        self.store.cache_doc("https://example.com/a", "body", later)
        doc = self.store.docs()[0]
        self.assertEqual(doc.fetched_at, "2020-01-01T00:00:00Z")
        self.assertTrue(doc.is_stale(later))

    def test_explicit_refresh_moves_the_clock_only(self) -> None:
        self.store.cache_doc("https://example.com/a", "body", "2020-01-01T00:00:00Z")
        result = self.store.cache_doc(
            "https://example.com/a", "body", "2026-01-01T00:00:00Z", refresh=True)
        self.assertEqual(result["reason"], "refreshed")
        self.assertFalse(result["changed"])
        self.assertEqual(result["doc"].fetched_at, "2026-01-01T00:00:00Z")


class TestFreshness(TempStore):
    def test_stale_is_detected_by_ttl(self) -> None:
        self.store.cache_doc("https://example.com/old", "old", "2026-01-01T00:00:00Z")
        self.store.cache_doc("https://example.com/new", "new", "2026-01-01T02:00:00Z")
        self.assertTrue(self.store.is_stale("https://example.com/old", "2026-01-01T03:00:00Z"))
        self.assertFalse(self.store.is_stale("https://example.com/new", "2026-01-01T02:30:00Z"))

    def test_freshness_report_lists_stale_rather_than_hiding_it(self) -> None:
        self.store.cache_doc("https://example.com/old", "old", "2026-01-01T00:00:00Z")
        self.store.cache_doc("https://example.com/new", "new", "2026-01-01T02:00:00Z")
        report = self.store.freshness("2026-01-01T02:30:00Z")
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["stale"], 1)
        self.assertEqual(report["fresh"], 1)
        self.assertTrue(all("stale" in d for d in report["docs"]))
        self.assertEqual(len(self.store.stale_docs("2026-01-01T02:30:00Z")), 1)

    def test_unreadable_timestamp_is_stale_not_an_exception(self) -> None:
        doc = Doc(url="https://example.com/x", content="c", fetched_at="not-a-date")
        self.assertTrue(doc.is_stale("2026-01-01T00:00:00Z"))
        self.assertFalse(doc.freshness("2026-01-01T00:00:00Z")["reason"] == "")

    def test_freshness_is_reported_on_a_lookup_hit(self) -> None:
        self.store.cache_doc("https://example.com/old", "text", "2026-01-01T00:00:00Z")
        hit = self.store.lookup("https://example.com/old", now="2026-01-01T05:00:00Z")
        self.assertTrue(hit["hit"])
        self.assertTrue(hit["stale"])
        self.assertIsNotNone(hit["freshness"])
        self.assertGreater(hit["freshness"]["age_seconds"], 0)

    def test_missing_doc_cannot_claim_freshness(self) -> None:
        with self.assertRaises(KnowledgeError) as ctx:
            self.store.is_stale("https://example.com/never-fetched")
        self.assertEqual(ctx.exception.code, "NO_SUCH_DOC")


class TestLookup(TempStore):
    def test_lookup_returns_the_cache_hit(self) -> None:
        self.store.cache_doc(TRACKED, "body of the guide", "2026-01-01T00:00:00Z")
        hit = self.store.lookup("https://docs.example.com/guide/?utm_source=x")
        self.assertTrue(hit["hit"])
        self.assertEqual(hit["matched_on"], "url")
        self.assertEqual(hit["doc"].content, "body of the guide")

    def test_second_identical_lookup_is_the_same_hit(self) -> None:
        self.store.cache_doc(
            "https://example.com/polyhaven",
            "api reference",
            "2026-01-01T00:00:00Z",
            title="polyhaven api docs",
        )
        first = self.store.lookup("polyhaven api docs")
        second = self.store.lookup("polyhaven api docs")
        self.assertTrue(first["hit"])
        self.assertEqual(first["url"], second["url"])
        self.assertEqual(first["doc"].url, second["doc"].url)
        self.assertEqual(len(self.store.docs()), 1)

    def test_lookup_miss_is_explicit(self) -> None:
        hit = self.store.lookup("https://example.com/never-fetched")
        self.assertFalse(hit["hit"])
        self.assertIsNone(hit["doc"])
        self.assertEqual(hit["score"], 0.0)

    def test_lookup_does_not_match_unrelated_documents(self) -> None:
        self.store.cache_doc("https://example.com/a", "quantum chromodynamics",
                             "2026-01-01T00:00:00Z")
        self.assertFalse(self.store.lookup("sourdough starter hydration")["hit"])


class TestRememberAndRecall(TempStore):
    def test_each_kind_comes_back_as_its_own_type(self) -> None:
        fact = self.store.remember("deploy target", "eu-west-1", "fact")
        state = self.store.remember("active branch", "feat/knowledge", "state")
        guess = self.store.remember("flaky gate", "the gate times out", "hypothesis")
        self.assertIsInstance(fact, Fact)
        self.assertIsInstance(state, State)
        self.assertIsInstance(guess, Hypothesis)
        self.assertNotIsInstance(guess, Fact)
        self.assertNotIsInstance(fact, Hypothesis)
        self.assertEqual(guess.kind, "hypothesis")

    def test_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(KnowledgeError) as ctx:
            self.store.remember("k", "v", "belief")
        self.assertEqual(ctx.exception.code, "BAD_KIND")

    def test_recall_filters_by_relevance_not_history(self) -> None:
        for i in range(30):
            self.store.remember(f"unrelated note {i}", f"filler value {i}", "fact")
        self.store.remember("deploy target region", "eu-west-1", "fact")
        results = self.store.recall("deploy target region")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["key"], "deploy target region")
        self.assertGreater(results[0]["score"], 0.0)

    def test_recall_on_an_unrelated_query_returns_nothing(self) -> None:
        self.store.remember("deploy target region", "eu-west-1", "fact")
        self.assertEqual(self.store.recall("wombat migration schedule"), [])

    def test_recall_filters_by_kind(self) -> None:
        self.store.remember("deploy target", "eu-west-1", "fact")
        self.store.remember("deploy target", "us-east-2", "state")
        self.store.remember("deploy target", "maybe asia", "hypothesis")
        facts = self.store.recall("deploy target", kind="fact")
        states = self.store.recall("deploy target", kind="state")
        self.assertEqual([r["kind"] for r in facts], ["fact"])
        self.assertEqual([r["kind"] for r in states], ["state"])
        self.assertEqual(len(self.store.recall("deploy target")), 3)

    def test_recall_labels_every_entry_with_its_kind(self) -> None:
        self.store.remember("deploy target", "eu-west-1", "hypothesis")
        result = self.store.recall("deploy target")[0]
        self.assertEqual(result["kind"], "hypothesis")
        self.assertIn("score", result)

    def test_key_hits_outrank_value_mentions(self) -> None:
        self.store.remember("deploy target", "eu-west-1", "fact")
        self.store.remember("scratch", "the deploy target is staging", "state")
        results = self.store.recall("deploy target")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["key"], "deploy target")

    def test_recall_respects_the_limit(self) -> None:
        self.store.remember("region a", "one", "fact")
        self.store.remember("region b", "two", "fact")
        self.assertEqual(len(self.store.recall("region", limit=1)), 1)

    def test_re_remembering_the_same_value_does_not_churn(self) -> None:
        self.store.remember("deploy target", "eu-west-1", "fact")
        before = self.raw("knowledge.jsonl")
        stamp = self.store.entries_path.stat().st_mtime_ns
        again = self.store.remember("deploy target", "eu-west-1", "fact")
        self.assertIsInstance(again, Fact)
        self.assertEqual(self.raw("knowledge.jsonl"), before)
        self.assertEqual(self.store.entries_path.stat().st_mtime_ns, stamp)

    def test_new_value_supersedes_the_old_one(self) -> None:
        self.store.remember("deploy target", "eu-west-1", "fact")
        self.store.remember("deploy target", "us-east-2", "fact")
        results = self.store.recall("deploy target")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "us-east-2")


class TestPromotion(TempStore):
    def test_hypothesis_cannot_be_promoted_without_evidence(self) -> None:
        guess = self.store.remember("gate is flaky", "it timed out twice", "hypothesis")
        with self.assertRaises(KnowledgeError) as ctx:
            self.store.promote(guess, "")
        self.assertEqual(ctx.exception.code, "NO_EVIDENCE")
        with self.assertRaises(KnowledgeError) as ctx2:
            self.store.promote(guess, "   ")
        self.assertEqual(ctx2.exception.code, "NO_EVIDENCE")
        self.assertEqual(len(self.store.hypotheses()), 1)
        self.assertEqual(self.store.facts(), [])

    def test_hypothesis_promotes_with_evidence(self) -> None:
        guess = self.store.remember("gate is flaky", "it timed out twice", "hypothesis")
        fact = self.store.promote(
            guess, "tests/test_gate.py::test_timeout reproduced twice, exit 1")
        self.assertIsInstance(fact, Fact)
        self.assertEqual(fact.key, "gate is flaky")
        self.assertEqual(fact.promoted_from, guess.id)
        self.assertEqual(self.store.hypotheses(), [])
        self.assertEqual(len(self.store.facts()), 1)
        self.assertEqual(self.store.recall("gate flaky")[0]["kind"], "fact")

    def test_a_fact_cannot_be_promoted(self) -> None:
        fact = self.store.remember("deploy target", "eu-west-1", "fact")
        with self.assertRaises(KnowledgeError) as ctx:
            self.store.promote(fact, "because I said so")
        self.assertEqual(ctx.exception.code, "NOT_A_HYPOTHESIS")

    def test_a_key_naming_a_fact_cannot_be_promoted(self) -> None:
        self.store.remember("deploy target", "eu-west-1", "fact")
        with self.assertRaises(KnowledgeError) as ctx:
            self.store.promote("deploy target", "evidence")
        self.assertEqual(ctx.exception.code, "NOT_A_HYPOTHESIS")

    def test_unknown_entry_cannot_be_promoted(self) -> None:
        with self.assertRaises(KnowledgeError) as ctx:
            self.store.promote("never remembered", "evidence")
        self.assertEqual(ctx.exception.code, "NO_SUCH_ENTRY")


class TestSecretHygiene(TempStore):
    SECRETS = {
        "openai": "sk-abcdefghijklmnopqrstuvwx",
        "github": "ghp_abcdefghijklmnopqrstuv",
        "slack": "xoxb-123456789012-abcdefghijkl",
        "bearer": "abcdefghijklmnopqrst",
        "env": "wJalrXUtnFEMIabcdefghijklmno",
        "password": "hunter2secret",
        "inline_env": "QWERTYsecretvalue123",
    }

    def _plant_secrets(self) -> None:
        self.store.remember(
            "auth notes",
            "OPENAI_API_KEY={openai} password={password}".format(**self.SECRETS),
            "state",
        )
        self.store.remember(
            "ci",
            "Authorization: Bearer {bearer}\nGITHUB_TOKEN={github}".format(**self.SECRETS),
            "fact",
        )
        self.store.cache_doc(
            "https://example.com/api",
            "curl -H 'Authorization: Bearer {bearer}' \\\n"
            "  https://api.example.com/v1 --key {openai}\n"
            "slack={slack}\n"
            "AWS_SECRET_ACCESS_KEY={env}\n"
            "EXTERNAL_SECRET={inline_env}\n".format(**self.SECRETS),
            "2026-01-01T00:00:00Z",
        )

    def test_no_secret_survives_in_the_raw_store(self) -> None:
        self._plant_secrets()
        for name in ("knowledge.jsonl", "docs.jsonl"):
            text = self.raw(name)
            self.assertTrue(text.strip(), f"{name} was not written")
            for label, secret in self.SECRETS.items():
                self.assertNotIn(secret, text, f"{label} secret leaked into {name}")

    def test_no_secret_survives_retrieval(self) -> None:
        self._plant_secrets()
        recalled = self.store.recall("auth notes password")
        self.assertTrue(recalled)
        for row in recalled:
            for secret in self.SECRETS.values():
                self.assertNotIn(secret, row["value"])
        self.assertIn("[REDACTED]", "".join(r["value"] for r in recalled))

        hit = self.store.lookup("https://example.com/api")
        self.assertTrue(hit["hit"])
        for secret in self.SECRETS.values():
            self.assertNotIn(secret, hit["doc"].content)

    def test_redaction_leaves_ordinary_text_alone(self) -> None:
        text = "the deploy target is eu-west-1 and the guide is at https://example.com/g"
        self.assertEqual(redact_secrets(text), text)

    def test_curl_style_password_is_redacted(self) -> None:
        cleaned = redact_secrets("curl -u alice:hunter2secret https://api.example.com")
        self.assertNotIn("hunter2secret", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_url_embedded_password_is_redacted(self) -> None:
        cleaned = redact_secrets("fetch https://alice:hunter2secret@example.com/x")
        self.assertNotIn("hunter2secret", cleaned)

    def test_project_memory_also_drops_addresses(self) -> None:
        self.store.remember("owner", "ping alice@example.com about it", "fact")
        self.assertNotIn("alice@example.com", self.raw("knowledge.jsonl"))
        self.assertEqual(redact_private("alice@example.com"), "[REDACTED]")

    def test_cached_document_may_keep_an_address_it_quotes(self) -> None:
        """Docs are copies of public text; only secrets are stripped there."""
        self.store.cache_doc(
            "https://example.com/contact", "support@example.com", "2026-01-01T00:00:00Z")
        self.assertIn("support@example.com", self.raw("docs.jsonl"))


class TestCapsAndPrune(TempStore):
    def test_oversized_transcript_is_refused(self) -> None:
        with self.assertRaises(KnowledgeError) as ctx:
            self.store.remember("huge", "x" * (MAX_VALUE_CHARS + 1), "fact")
        self.assertEqual(ctx.exception.code, "VALUE_TOO_LARGE")
        self.assertEqual(self.raw("knowledge.jsonl"), "")

    def test_oversized_document_is_capped_and_flagged(self) -> None:
        result = self.store.cache_doc(
            "https://example.com/big",
            "y" * (MAX_DOC_CHARS + 5000),
            "2026-01-01T00:00:00Z",
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["doc"].content), MAX_DOC_CHARS)
        # Bounded by a constant: content cap + the capped search index + JSON
        # scaffolding. That bound is the whole point of the cap.
        self.assertLessEqual(len(self.raw("docs.jsonl")),
                             MAX_DOC_CHARS + MAX_SEARCH_TEXT_CHARS + 2000)

    def test_document_cache_evicts_the_stalest_at_cap(self) -> None:
        store = Knowledge(self.root, max_docs=2)
        store.cache_doc("https://example.com/a", "a", "2026-01-01T00:00:00Z")
        store.cache_doc("https://example.com/b", "b", "2026-01-02T00:00:00Z")
        result = store.cache_doc("https://example.com/c", "c", "2026-01-03T00:00:00Z")
        self.assertEqual(result["evicted"], ["https://example.com/a"])
        urls = sorted(d.url for d in store.docs())
        self.assertEqual(urls, ["https://example.com/b", "https://example.com/c"])

    def test_memory_refuses_to_grow_past_its_cap(self) -> None:
        store = Knowledge(self.root, max_entries=2)
        store.remember("a", "1", "fact")
        store.remember("b", "2", "fact")
        with self.assertRaises(KnowledgeError) as ctx:
            store.remember("c", "3", "fact")
        self.assertEqual(ctx.exception.code, "STORE_FULL")

    def test_prune_drops_expired_state_and_keeps_facts(self) -> None:
        self.store.remember("deploy target", "eu-west-1", "fact")
        self.store.remember("active branch", "feat/x", "state")
        self.store.remember("guess", "maybe", "hypothesis")
        report = self.store.prune(older_than=0.0)
        self.assertEqual(report["entries_removed"], 1)
        self.assertEqual(report["removed_entries"][0]["reason"], "expired_state")
        self.assertEqual(report["remaining"]["by_kind"]["fact"], 1)
        self.assertEqual(report["remaining"]["by_kind"]["state"], 0)
        self.assertEqual(report["remaining"]["by_kind"]["hypothesis"], 1)

    def test_prune_frees_room_for_new_memory(self) -> None:
        store = Knowledge(self.root, max_entries=2)
        store.remember("state a", "1", "state")
        store.remember("state b", "2", "state")
        with self.assertRaises(KnowledgeError):
            store.remember("fact c", "3", "fact")
        store.prune(older_than=0.0)
        entry = store.remember("fact c", "3", "fact")
        self.assertEqual(entry.key, "fact c")

    def test_prune_evicts_state_before_facts_under_a_tighter_cap(self) -> None:
        self.store.remember("keep me", "truth", "fact")
        self.store.remember("scratch a", "1", "state")
        self.store.remember("scratch b", "2", "state")
        report = self.store.prune(max_entries=1, older_than=10 ** 9)
        self.assertEqual(report["remaining"]["entries"], 1)
        self.assertEqual(report["remaining"]["by_kind"]["fact"], 1)
        self.assertEqual(self.store.facts()[0].key, "keep me")

    def test_prune_merges_hand_written_duplicate_documents(self) -> None:
        """Defensive: an older build or a hand edit can leave two copies."""
        self.root.mkdir(parents=True, exist_ok=True)
        body = "identical body"
        records = [
            {"url": "https://example.com/one", "content": body,
             "hash": content_hash(body), "fetched_at": "2026-01-01T00:00:00Z",
             "ttl_seconds": 3600.0},
            {"url": "https://example.com/two", "content": body,
             "hash": content_hash(body), "fetched_at": "2026-01-02T00:00:00Z",
             "ttl_seconds": 3600.0},
        ]
        (self.root / "docs.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        report = self.store.prune()
        self.assertEqual(report["docs_removed"], 1)
        self.assertEqual(report["merged_docs"][0]["into"], "https://example.com/two")
        docs = self.store.docs()
        self.assertEqual(len(docs), 1)
        self.assertIn("https://example.com/one", docs[0].aliases)
        self.assertTrue(self.store.lookup("https://example.com/one")["hit"])

    def test_store_creates_a_gitignore_guard(self) -> None:
        self.store.remember("k", "v", "fact")
        self.assertEqual((self.root / ".gitignore").read_text(encoding="utf-8"), "*\n")


class TestRelevanceFunction(unittest.TestCase):
    def test_unrelated_text_scores_zero(self) -> None:
        self.assertEqual(relevance("alpha beta", key_text="gamma", value_text="delta"), 0.0)

    def test_key_matches_outweigh_value_matches(self) -> None:
        on_key = relevance("deploy target", key_text="deploy target", value_text="")
        on_value = relevance("deploy target", key_text="notes",
                             value_text="the deploy target is staging")
        self.assertGreater(on_key, on_value)

    def test_empty_query_scores_zero(self) -> None:
        self.assertEqual(relevance("", key_text="deploy target", value_text="x"), 0.0)


class TestModuleSurface(unittest.TestCase):
    def test_bad_kind_from_the_constructor_path_is_still_typed(self) -> None:
        with self.assertRaises(KnowledgeError):
            Knowledge().recall("anything", kind="rumour")

    def test_defaults_are_sane(self) -> None:
        self.assertEqual(DEFAULT_TTL_SECONDS, 7 * 24 * 3600.0)
        self.assertGreater(MAX_DOC_CHARS, MAX_VALUE_CHARS)


if __name__ == "__main__":
    unittest.main()

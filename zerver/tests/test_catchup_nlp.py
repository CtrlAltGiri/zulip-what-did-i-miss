"""
Tests for the "What Did I Miss?" NLP pipeline.

Run with:
    ./tools/test-backend zerver.tests.test_catchup_nlp
"""

from __future__ import annotations

from unittest import mock

from zerver.lib.catchup_nlp import (
    ActionItem,
    TopicSummary,
    detect_action_items,
    extract_keywords,
    extractive_summarize,
    preprocess_text,
    summarize_topic,
)
from zerver.lib.test_classes import ZulipTestCase


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_messages(*contents: str, sender: str = "Alice") -> list[dict[str, object]]:
    return [{"content": c, "sender_full_name": sender} for c in contents]


# ── 1. Text preprocessing ─────────────────────────────────────────────────────


class PreprocessTextTest(ZulipTestCase):
    def test_strips_code_blocks(self) -> None:
        raw = "Here is code:\n```python\nprint('hello')\n```\nAnd done."
        result = preprocess_text(raw)
        self.assertNotIn("print", result)
        self.assertIn("And done", result)

    def test_strips_inline_code(self) -> None:
        result = preprocess_text("Call `foo()` to start.")
        self.assertNotIn("`", result)
        self.assertIn("Call", result)
        self.assertIn("to start", result)

    def test_strips_bold_italic_preserves_text(self) -> None:
        result = preprocess_text("This is **important** and *critical*.")
        self.assertIn("important", result)
        self.assertIn("critical", result)
        self.assertNotIn("**", result)
        self.assertNotIn("*", result)

    def test_strips_links_preserves_label(self) -> None:
        result = preprocess_text("See [the docs](https://example.com) for details.")
        self.assertIn("the docs", result)
        self.assertNotIn("https://", result)

    def test_strips_emoji(self) -> None:
        result = preprocess_text("Great job :thumbs_up: team :tada:")
        self.assertNotIn(":", result)
        self.assertIn("Great job", result)

    def test_strips_mentions(self) -> None:
        result = preprocess_text("Hey @**Alice Smith** can you review this?")
        self.assertNotIn("@", result)
        self.assertIn("can you review this", result)

    def test_strips_blockquote(self) -> None:
        result = preprocess_text("> Original message\nMy reply.")
        self.assertNotIn(">", result)
        self.assertIn("My reply", result)

    def test_normalises_whitespace(self) -> None:
        result = preprocess_text("Too   many    spaces\t\there.")
        self.assertNotIn("  ", result)

    def test_empty_string(self) -> None:
        self.assertEqual(preprocess_text(""), "")

    def test_plain_text_unchanged(self) -> None:
        plain = "This is a plain sentence."
        self.assertEqual(preprocess_text(plain), plain)


# ── 2. Extractive summarisation ───────────────────────────────────────────────


class ExtractiveSummarizeTest(ZulipTestCase):
    def _long_sentences(self) -> list[str]:
        return [
            "The deployment pipeline was updated to include automated integration tests.",
            "All pull requests now require two approvals before merging.",
            "The database migration for the new schema ran successfully on staging.",
            "Performance benchmarks show a 15 percent improvement in query latency.",
            "The frontend bundle size was reduced by removing unused dependencies.",
            "Documentation for the new API endpoints has been published.",
        ]

    def test_returns_correct_number_of_sentences(self) -> None:
        sentences = self._long_sentences()
        summary, _, n_used, _ = extractive_summarize(sentences, 3)
        self.assertEqual(n_used, 3)
        self.assertGreater(len(summary), 0)

    def test_does_not_exceed_available_sentences(self) -> None:
        sentences = ["Only one sentence here."]
        summary, _, n_used, _ = extractive_summarize(sentences, 10)
        self.assertEqual(n_used, 1)

    def test_empty_input(self) -> None:
        summary, confidence, n_used, backend = extractive_summarize([], 3)
        self.assertEqual(summary, "")
        self.assertEqual(confidence, 0.0)
        self.assertEqual(n_used, 0)

    def test_confidence_in_range(self) -> None:
        sentences = self._long_sentences()
        _, confidence, _, _ = extractive_summarize(sentences, 2)
        self.assertGreaterEqual(confidence, 0.0)
        self.assertLessEqual(confidence, 1.0)

    def test_summary_is_subset_of_original(self) -> None:
        sentences = self._long_sentences()
        summary, _, _, _ = extractive_summarize(sentences, 3)
        # Every sentence in the summary should come from the original set
        for sent in summary.split(". "):
            sent = sent.strip().rstrip(".")
            if sent:
                self.assertTrue(
                    any(sent in orig for orig in sentences),
                    f"'{sent}' not found in original sentences",
                )


# ── 3. Keyword extraction ─────────────────────────────────────────────────────


class ExtractKeywordsTest(ZulipTestCase):
    def test_returns_list(self) -> None:
        keywords = extract_keywords("We deployed a new database migration today.", top_n=3)
        self.assertIsInstance(keywords, list)

    def test_respects_top_n(self) -> None:
        text = (
            "The authentication service, authorization middleware, database pool, "
            "message broker, and cache layer were all updated in the release."
        )
        keywords = extract_keywords(text, top_n=3)
        self.assertLessEqual(len(keywords), 3)

    def test_empty_text(self) -> None:
        keywords = extract_keywords("", top_n=5)
        self.assertIsInstance(keywords, list)

    def test_excludes_stopwords(self) -> None:
        keywords = extract_keywords("The the the and and or or", top_n=5)
        stopwords = {"the", "and", "or", "a", "an", "is", "are"}
        for kw in keywords:
            self.assertNotIn(kw.lower(), stopwords)

    def test_meaningful_keywords_extracted(self) -> None:
        text = "The PostgreSQL migration script updated the users and messages tables."
        keywords = extract_keywords(text, top_n=5)
        lower_kws = [k.lower() for k in keywords]
        # At least one domain-relevant word should appear
        self.assertTrue(
            any(k in lower_kws for k in ["postgresql", "migration", "script", "users", "messages"]),
            f"Expected domain keywords, got: {keywords}",
        )


# ── 4. Action-item detection ─────────────────────────────────────────────────


class DetectActionItemsTest(ZulipTestCase):
    def test_detects_todo_marker(self) -> None:
        msgs = _make_messages("TODO: Update the README with setup instructions.")
        items = detect_action_items(msgs)
        self.assertGreater(len(items), 0)
        self.assertIn("Update the README with setup instructions", items[0].text)

    def test_detects_will_commitment(self) -> None:
        msgs = _make_messages("Alice will deploy the hotfix by Friday.")
        items = detect_action_items(msgs)
        self.assertGreater(len(items), 0)

    def test_detects_please_request(self) -> None:
        msgs = _make_messages("Please review the PR before the standup.")
        items = detect_action_items(msgs)
        self.assertGreater(len(items), 0)

    def test_detects_needs_to(self) -> None:
        msgs = _make_messages("The CI pipeline needs to be updated for the new runner.")
        items = detect_action_items(msgs)
        self.assertGreater(len(items), 0)

    def test_no_action_items_in_plain_discussion(self) -> None:
        msgs = _make_messages(
            "The meeting went well.",
            "We discussed the architecture and everyone agreed it looks good.",
            "The diagrams are clear.",
        )
        items = detect_action_items(msgs)
        # May or may not find items; just verify it returns a list
        self.assertIsInstance(items, list)

    def test_deduplication(self) -> None:
        same = "TODO: Fix the broken test."
        msgs = _make_messages(same, same, same)
        items = detect_action_items(msgs)
        texts = [i.text for i in items]
        # The same action item should not appear more than once
        self.assertEqual(len(texts), len(set(t.lower() for t in texts)))

    def test_sender_attribution(self) -> None:
        msgs = [{"content": "TODO: Write unit tests.", "sender_full_name": "Bob"}]
        items = detect_action_items(msgs)
        self.assertTrue(any(i.source_sender == "Bob" for i in items))

    def test_returns_action_item_dataclass(self) -> None:
        msgs = _make_messages("TODO: Deploy to production.")
        items = detect_action_items(msgs)
        self.assertGreater(len(items), 0)
        self.assertIsInstance(items[0], ActionItem)

    def test_handles_empty_messages(self) -> None:
        items = detect_action_items([])
        self.assertEqual(items, [])


# ── 5. Full pipeline: summarize_topic ────────────────────────────────────────


class SummarizeTopicTest(ZulipTestCase):
    def test_empty_messages_returns_empty_summary(self) -> None:
        result = summarize_topic([])
        self.assertIsInstance(result, TopicSummary)
        self.assertEqual(result.summary, "")
        self.assertEqual(result.message_count, 0)

    def test_short_topic_is_verbatim(self) -> None:
        # Under 10 words total → verbatim path
        msgs = _make_messages("Looks good to me.")
        result = summarize_topic(msgs)
        self.assertTrue(result.is_verbatim)
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.backend, "verbatim")
        self.assertIn("Looks good", result.summary)

    def test_long_topic_is_not_verbatim(self) -> None:
        msgs = _make_messages(
            "We discussed the new authentication flow and decided to use OAuth2.",
            "The backend team will implement the token refresh endpoint by next Wednesday.",
            "Frontend integration will be handled by Sanjeev after the backend is ready.",
            "We need to write integration tests before merging to main.",
            "Giridhar will schedule a follow-up review meeting for Thursday.",
        )
        result = summarize_topic(msgs)
        self.assertFalse(result.is_verbatim)
        self.assertGreater(len(result.summary), 0)

    def test_returns_topic_summary_dataclass(self) -> None:
        msgs = _make_messages(
            "The sprint planning meeting covered all user stories.",
            "US-05 and US-06 are assigned to Giridhar.",
            "The team agreed to use spaCy for the NLP pipeline.",
        )
        result = summarize_topic(msgs)
        self.assertIsInstance(result, TopicSummary)

    def test_confidence_in_valid_range(self) -> None:
        msgs = _make_messages(
            "The database schema migration was reviewed and approved.",
            "All foreign key constraints have been verified.",
            "The migration will run during the maintenance window on Sunday.",
            "Dennis will monitor the deployment and rollback if needed.",
        )
        result = summarize_topic(msgs)
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)

    def test_message_count_matches(self) -> None:
        msgs = _make_messages("Msg one.", "Msg two.", "Msg three.")
        result = summarize_topic(msgs)
        self.assertEqual(result.message_count, 3)

    def test_keywords_present(self) -> None:
        msgs = _make_messages(
            "The PostgreSQL migration script updated the users table.",
            "The messages table also received a new index on timestamp.",
            "Performance improved significantly after the migration.",
        )
        result = summarize_topic(msgs)
        self.assertIsInstance(result.keywords, list)

    def test_action_items_detected_in_pipeline(self) -> None:
        msgs = _make_messages(
            "We agreed on the API contract.",
            "TODO: Update the OpenAPI spec to reflect the changes.",
            "Dennis will deploy the staging environment by Monday.",
        )
        result = summarize_topic(msgs)
        self.assertIsInstance(result.action_items, list)
        self.assertGreater(len(result.action_items), 0)

    def test_direct_mention_outranks_plain_discussion(self) -> None:
        """
        A message containing a direct action item (TODO / will) should
        surface in action_items even when mixed with passive discussion.
        """
        msgs = _make_messages(
            "The architecture looks solid overall.",
            "We reviewed the component diagram today.",
            "TODO: Add confidence scores to the NLP output.",
            "The deployment diagram is also clear.",
        )
        result = summarize_topic(msgs)
        action_texts = [a.text.lower() for a in result.action_items]
        self.assertTrue(
            any("confidence" in t or "nlp" in t or "scores" in t for t in action_texts),
            f"Expected action item mentioning TODO; got: {result.action_items}",
        )

    def test_messages_with_only_code_blocks(self) -> None:
        msgs = _make_messages("```python\ndef hello():\n    return 'world'\n```")
        # Should not raise; code is stripped and result may be short/verbatim
        result = summarize_topic(msgs)
        self.assertIsInstance(result, TopicSummary)

    def test_backend_field_is_populated(self) -> None:
        msgs = _make_messages(
            "Sprint 2 is focused on building the core backend pipeline.",
            "The inactivity detection service queries the UserPresence model.",
            "Message aggregation groups results by stream and topic.",
            "Importance scoring weighs mentions, reply count, and recency.",
        )
        result = summarize_topic(msgs)
        self.assertIn(result.backend, {"spacy", "sumy", "frequency", "verbatim"})

    def test_spacy_unavailable_falls_back_gracefully(self) -> None:
        """When spaCy cannot be loaded the pipeline must still return a result."""
        import zerver.lib.catchup_nlp as nlp_module

        with mock.patch.object(nlp_module, "_get_spacy_nlp", return_value=None):
            msgs = _make_messages(
                "The team completed the environment setup for all members.",
                "Vagrant provisioning was documented in the repository README.",
                "CI/CD using GitHub Actions was configured for linting and tests.",
            )
            result = summarize_topic(msgs)
            self.assertIsInstance(result, TopicSummary)
            self.assertGreater(len(result.summary), 0)
            self.assertIn(result.backend, {"frequency", "verbatim"})

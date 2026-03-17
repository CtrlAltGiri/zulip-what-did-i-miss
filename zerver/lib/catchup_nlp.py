"""
NLP pipeline for the "What Did I Miss?" catch-up feature.

Pipeline stages (per system design, Sprint 3):
  1. Text preprocessing  – strip Zulip markdown, normalise whitespace
  2. Extractive summarisation – spaCy (primary) → sumy LexRank (fallback)
                              → frequency-based TF-IDF (final fallback)
  3. Keyword extraction  – noun-chunks / named entities (spaCy) or word-freq
  4. Action-item detection – regex pattern matching
  5. Short-message handling – messages < 10 words are returned verbatim
  6. Confidence scoring  – returned alongside every summary
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Zulip markdown patterns to strip before NLP ──────────────────────────────
_MD_CODE_BLOCK = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_MD_INLINE_CODE = re.compile(r"`[^`]+`")
_MD_BOLD_ITALIC = re.compile(r"\*{1,3}(.+?)\*{1,3}")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_EMOJI = re.compile(r":[a-z0-9_+\-]+:")
_MD_MENTION = re.compile(r"@\*{0,2}[^*\n]+\*{0,2}")
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BLOCKQUOTE = re.compile(r"^>\s*", re.MULTILINE)
_MD_HR = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")

# ── Action-item detection patterns ───────────────────────────────────────────
_ACTION_PATTERNS = [
    # TODO / FIXME / ACTION markers
    re.compile(r"(?:TODO|FIXME|ACTION\s*ITEM|ACTION)[:\s]+(.+)", re.IGNORECASE),
    # future-tense commitment: "will <verb>", "going to <verb>"
    re.compile(
        r"(?:(?:@\S+\s+)?(?:will|going to|plans? to|intends? to)\s+\w[\w\s]{2,40})",
        re.IGNORECASE,
    ),
    # obligation: "needs to", "should", "must"
    re.compile(
        r"(?:(?:@\S+\s+)?(?:needs? to|should|must|have to|has to)\s+\w[\w\s]{2,40})",
        re.IGNORECASE,
    ),
    # imperative requests: "please …", "make sure …", "don't forget …"
    re.compile(
        r"(?:please|make sure|ensure|don't forget|remember to)\s+\w[\w\s]{2,40}",
        re.IGNORECASE,
    ),
    # explicit deadlines: "by <day/date>"
    re.compile(
        r"(?:by\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|eod|eow|tomorrow|next week)[\w\s,]*)",
        re.IGNORECASE,
    ),
]

# ── Sentence boundary splitter (lightweight, no NLTK required) ───────────────
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

SHORT_MESSAGE_WORD_THRESHOLD = 10


# ── Public dataclasses ────────────────────────────────────────────────────────


@dataclass
class ActionItem:
    text: str
    source_sender: str = ""


@dataclass
class TopicSummary:
    """Complete NLP output for a single stream/topic group."""

    summary: str
    confidence: float  # 0.0 – 1.0
    keywords: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    is_verbatim: bool = False  # True when short-message path was taken
    message_count: int = 0
    sentences_used: int = 0
    sentences_total: int = 0
    backend: str = "frequency"  # "spacy" | "sumy" | "frequency"


# ── Lazy spaCy loader ─────────────────────────────────────────────────────────

_spacy_nlp: Any = None
_spacy_load_attempted = False


def _get_spacy_nlp() -> Any:
    """Return a loaded spaCy nlp object, or None if unavailable."""
    global _spacy_nlp, _spacy_load_attempted
    if _spacy_load_attempted:
        return _spacy_nlp
    _spacy_load_attempted = True
    try:
        import spacy  # noqa: PLC0415

        _spacy_nlp = spacy.load("en_core_web_sm")
        logger.debug("spaCy en_core_web_sm loaded successfully.")
    except Exception as exc:  # pragma: no cover
        logger.warning("spaCy unavailable (%s); will use fallback summariser.", exc)
        _spacy_nlp = None
    return _spacy_nlp


# ── Text preprocessing ────────────────────────────────────────────────────────


def preprocess_text(raw: str) -> str:
    """
    Strip Zulip-flavoured Markdown and normalise whitespace, producing
    plain prose suitable for sentence-level NLP.
    """
    text = _MD_CODE_BLOCK.sub(" ", raw)
    text = _MD_INLINE_CODE.sub(" ", text)
    text = _MD_BOLD_ITALIC.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_EMOJI.sub(" ", text)
    text = _MD_MENTION.sub(" ", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BLOCKQUOTE.sub("", text)
    text = _MD_HR.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text


def _word_count(text: str) -> int:
    return len(text.split())


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences using a simple regex heuristic."""
    parts = _SENTENCE_SPLIT.split(text)
    return [s.strip() for s in parts if s.strip()]


# ── Frequency-based sentence scoring (always-available fallback) ──────────────


def _stopwords() -> frozenset[str]:
    return frozenset(
        {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
            "for", "of", "with", "by", "from", "is", "are", "was", "were",
            "be", "been", "being", "have", "has", "had", "do", "does", "did",
            "will", "would", "could", "should", "may", "might", "shall",
            "it", "its", "this", "that", "these", "those", "i", "we",
            "you", "he", "she", "they", "them", "their", "our", "your",
            "my", "me", "us", "so", "if", "as", "not", "also", "just",
            "about", "up", "out", "then", "than", "there", "here", "when",
        }
    )


def _score_sentences_frequency(sentences: list[str]) -> list[float]:
    """
    Score each sentence by normalised word frequency (TF-IDF proxy).
    Returns a parallel list of scores in [0, 1].
    """
    stops = _stopwords()
    words: list[str] = []
    for s in sentences:
        words.extend(
            w.lower() for w in re.findall(r"\b[a-z]{2,}\b", s.lower()) if w not in stops
        )
    if not words:
        return [0.0] * len(sentences)

    freq = Counter(words)
    max_freq = max(freq.values())
    normalised = {w: c / max_freq for w, c in freq.items()}

    scores: list[float] = []
    for sentence in sentences:
        tokens = [
            w.lower()
            for w in re.findall(r"\b[a-z]{2,}\b", sentence.lower())
            if w.lower() not in stops
        ]
        score = sum(normalised.get(t, 0.0) for t in tokens) / max(len(tokens), 1)
        scores.append(score)
    return scores


# ── spaCy-based sentence scoring ─────────────────────────────────────────────


def _score_sentences_spacy(sentences: list[str], nlp: Any) -> list[float]:
    """
    Score sentences using spaCy token vectors (word-embedding centroid similarity).
    Falls back to frequency scoring if vectors are unavailable.
    """
    docs = list(nlp.pipe(sentences))
    # Check if the model has word vectors
    if not docs or not docs[0].has_vector:
        return _score_sentences_frequency(sentences)

    # Build a pseudo-document vector as the centroid of all sentence vectors
    import numpy as np  # spaCy already requires numpy

    all_vecs = [doc.vector for doc in docs if doc.has_vector]
    if not all_vecs:
        return _score_sentences_frequency(sentences)

    centroid = np.mean(all_vecs, axis=0)
    centroid_norm = np.linalg.norm(centroid)
    if centroid_norm == 0:
        return _score_sentences_frequency(sentences)

    scores: list[float] = []
    for doc in docs:
        if doc.has_vector and np.linalg.norm(doc.vector) > 0:
            sim = float(
                np.dot(doc.vector, centroid)
                / (np.linalg.norm(doc.vector) * centroid_norm)
            )
            scores.append(max(0.0, sim))
        else:
            scores.append(0.0)
    return scores


# ── sumy-based summarisation ─────────────────────────────────────────────────


def _summarise_sumy(text: str, n_sentences: int) -> tuple[str, float] | None:
    """
    Attempt extractive summarisation via sumy LexRank.
    Returns (summary_text, confidence) or None if sumy is unavailable.
    """
    try:
        from sumy.nlp.tokenizers import Tokenizer  # noqa: PLC0415
        from sumy.parsers.plaintext import PlaintextParser  # noqa: PLC0415
        from sumy.summarizers.lex_rank import LexRankSummarizer  # noqa: PLC0415

        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summarizer = LexRankSummarizer()
        selected = summarizer(parser.document, sentences_count=n_sentences)

        if not selected:
            return None

        summary = " ".join(str(s) for s in selected)
        # Confidence proxy: ratio of selected sentences to total
        total = len(list(parser.document.sentences))
        confidence = _clamp(1.0 - (len(selected) / max(total, 1)), 0.2, 0.95)
        return summary, confidence

    except Exception as exc:  # pragma: no cover
        logger.debug("sumy failed: %s", exc)
        return None


# ── Confidence calculation ────────────────────────────────────────────────────


def _calculate_confidence(
    selected_scores: list[float], all_scores: list[float]
) -> float:
    """
    Confidence reflects how much the selected sentences stand out.
    A high score means the chosen sentences are clearly more important
    than the average; a low score means all sentences are equally bland.
    """
    if not all_scores:
        return 0.0
    mean_all = sum(all_scores) / len(all_scores)
    if mean_all == 0:
        return 0.0
    mean_selected = sum(selected_scores) / len(selected_scores) if selected_scores else 0.0
    raw = mean_selected / (mean_all + 1e-9)
    # Normalise: 1.0 → no discrimination, higher → more confident
    confidence = _clamp((raw - 1.0) / 2.0 + 0.5, 0.1, 0.98)
    return round(confidence, 3)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ── Keyword extraction ────────────────────────────────────────────────────────


def _extract_keywords_spacy(text: str, nlp: Any, top_n: int = 5) -> list[str]:
    """Extract keywords using spaCy noun chunks and named entities."""
    doc = nlp(text)
    candidates: list[str] = []

    # Named entities (highest priority)
    for ent in doc.ents:
        if ent.label_ not in {"DATE", "TIME", "CARDINAL", "ORDINAL", "PERCENT", "MONEY"}:
            candidates.append(ent.text.strip())

    # Noun chunks
    for chunk in doc.noun_chunks:
        root = chunk.root
        if root.pos_ in {"NOUN", "PROPN"} and not root.is_stop:
            candidates.append(chunk.text.strip())

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        key = c.lower()
        if key not in seen and len(c) > 1:
            seen.add(key)
            unique.append(c)

    return unique[:top_n]


def _extract_keywords_frequency(text: str, top_n: int = 5) -> list[str]:
    """Frequency-based keyword extraction (fallback)."""
    stops = _stopwords()
    tokens = [
        w.lower()
        for w in re.findall(r"\b[a-zA-Z]{3,}\b", text)
        if w.lower() not in stops
    ]
    freq = Counter(tokens)
    return [word for word, _ in freq.most_common(top_n)]


def extract_keywords(text: str, top_n: int = 5) -> list[str]:
    """Public entry point for keyword extraction."""
    nlp = _get_spacy_nlp()
    if nlp is not None:
        try:
            return _extract_keywords_spacy(text, nlp, top_n)
        except Exception:  # pragma: no cover
            pass
    return _extract_keywords_frequency(text, top_n)


# ── Action-item detection ─────────────────────────────────────────────────────


def detect_action_items(messages: list[dict[str, Any]]) -> list[ActionItem]:
    """
    Scan each message for action-item patterns and return a deduplicated list.
    Each message dict must have at least a 'content' key and optionally
    'sender_full_name'.
    """
    results: list[ActionItem] = []
    seen_texts: set[str] = set()

    for msg in messages:
        raw_content = msg.get("content", "")
        sender = msg.get("sender_full_name", "")
        clean = preprocess_text(raw_content)

        for pattern in _ACTION_PATTERNS:
            for match in pattern.finditer(clean):
                # Grab either the first capture group or the full match
                text = (match.group(1) if match.lastindex else match.group(0)).strip()
                # Truncate very long matches
                text = text[:120].rstrip(",;:")
                key = text.lower()
                if key and key not in seen_texts:
                    seen_texts.add(key)
                    results.append(ActionItem(text=text, source_sender=sender))

    return results


# ── Core summarisation pipeline ───────────────────────────────────────────────


def _n_summary_sentences(message_count: int) -> int:
    """
    Determine how many sentences to include in the summary based on
    conversation length.  Mirrors get_max_summary_length() for LLM path.
    """
    return min(6, 3 + int((message_count - 1) / 8))


def extractive_summarize(
    sentences: list[str],
    n_sentences: int,
    *,
    backend_hint: str = "auto",
) -> tuple[str, float, int, str]:
    """
    Select the most important `n_sentences` from `sentences` and return
    (summary_text, confidence, sentences_used, backend_name).
    """
    if not sentences:
        return "", 0.0, 0, "none"

    n = min(n_sentences, len(sentences))

    # --- Try spaCy ---
    nlp = _get_spacy_nlp() if backend_hint in ("auto", "spacy") else None
    scores: list[float] | None = None
    backend = "frequency"

    if nlp is not None:
        try:
            scores = _score_sentences_spacy(sentences, nlp)
            backend = "spacy"
        except Exception as exc:  # pragma: no cover
            logger.debug("spaCy scoring failed: %s", exc)

    if scores is None:
        scores = _score_sentences_frequency(sentences)

    # Rank and select, preserving original order
    ranked = sorted(range(len(sentences)), key=lambda i: scores[i], reverse=True)
    selected_indices = sorted(ranked[:n])
    selected_sentences = [sentences[i] for i in selected_indices]
    selected_scores = [scores[i] for i in selected_indices]

    summary = " ".join(selected_sentences)
    confidence = _calculate_confidence(selected_scores, scores)

    return summary, confidence, len(selected_sentences), backend


# ── Public top-level pipeline ─────────────────────────────────────────────────


def summarize_topic(messages: list[dict[str, Any]]) -> TopicSummary:
    """
    Full NLP pipeline for a list of Zulip messages belonging to a single
    stream/topic group.

    Each message dict is expected to match the shape returned by
    `messages_for_ids()`: at minimum `content` and optionally
    `sender_full_name`.

    Returns a :class:`TopicSummary` with summary text, confidence score,
    keywords, and action items.
    """
    if not messages:
        return TopicSummary(
            summary="",
            confidence=0.0,
            message_count=0,
        )

    # ── 1. Collect and preprocess all text ────────────────────────────────────
    cleaned_texts: list[str] = []
    for msg in messages:
        cleaned = preprocess_text(msg.get("content", ""))
        if cleaned:
            cleaned_texts.append(cleaned)

    combined_text = " ".join(cleaned_texts)
    total_word_count = _word_count(combined_text)

    # ── 2. Short-message path: return verbatim ────────────────────────────────
    if total_word_count < SHORT_MESSAGE_WORD_THRESHOLD:
        keywords = extract_keywords(combined_text, top_n=3)
        action_items = detect_action_items(messages)
        return TopicSummary(
            summary=combined_text,
            confidence=1.0,  # verbatim → perfect fidelity
            keywords=keywords,
            action_items=action_items,
            is_verbatim=True,
            message_count=len(messages),
            sentences_used=1,
            sentences_total=1,
            backend="verbatim",
        )

    # ── 3. Split combined text into sentences ─────────────────────────────────
    sentences = _split_sentences(combined_text)
    if not sentences:
        sentences = [combined_text]

    n_target = _n_summary_sentences(len(messages))

    # ── 4. Try sumy first when spaCy vectors are unavailable ──────────────────
    #      (en_core_web_sm has no vectors; md/lg models do)
    nlp = _get_spacy_nlp()
    use_sumy = nlp is not None and not nlp("test").has_vector

    summary_text = ""
    confidence = 0.0
    sentences_used = 0
    backend = "frequency"

    if use_sumy:
        result = _summarise_sumy(combined_text, n_target)
        if result is not None:
            summary_text, confidence = result
            sentences_used = n_target
            backend = "sumy"

    if not summary_text:
        summary_text, confidence, sentences_used, backend = extractive_summarize(
            sentences, n_target
        )

    # ── 5. Keyword extraction ─────────────────────────────────────────────────
    keywords = extract_keywords(combined_text, top_n=5)

    # ── 6. Action-item detection ──────────────────────────────────────────────
    action_items = detect_action_items(messages)

    return TopicSummary(
        summary=summary_text,
        confidence=confidence,
        keywords=keywords,
        action_items=action_items,
        is_verbatim=False,
        message_count=len(messages),
        sentences_used=sentences_used,
        sentences_total=len(sentences),
        backend=backend,
    )

"""
US-07: Importance Scoring Algorithm
US-08: Context Linking

Scores and ranks topics for the catch-up view based on:
  - Direct @-mentions of the user        (weight 5.0)
  - @all / wildcard mentions             (weight 3.0)
  - Sender diversity                     (weight 1.5)
  - Reply count (message volume)         (weight 1.0)
  - Reactions                            (weight 0.5)
  - Recency                              (weight 0.5)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from zerver.lib.url_encoding import encode_channel, encode_hash_component

# ── Scoring weights (per spec) ────────────────────────────────────────────────
W_DIRECT_MENTION    = 5.0
W_WILDCARD_MENTION  = 3.0
W_SENDER_DIVERSITY  = 1.5
W_REPLY_COUNT       = 1.0
W_REACTIONS         = 0.5
W_RECENCY           = 0.5

DEFAULT_TOP_N = 20

_WILDCARD_MENTION = re.compile(r"@\*\*(all|everyone|channel|topic|stream)\*\*", re.IGNORECASE)


@dataclass
class ScoredTopic:
    stream_id: int
    stream_name: str
    topic: str
    score: float
    message_count: int
    sender_count: int
    first_message_id: int
    latest_message_id: int
    narrow_url: str
    sample_messages: list[dict[str, Any]] = field(default_factory=list)
    has_mention: bool = False
    has_wildcard_mention: bool = False
    reaction_count: int = 0


def _narrow_url(stream_id: int, stream_name: str, topic: str) -> str:
    """Build a Zulip deep-link URL for a stream/topic narrow."""
    return f"#narrow/{encode_channel(stream_id, stream_name, with_operator=True)}/topic/{encode_hash_component(topic)}"


def score_topics(
    messages: list[dict[str, Any]],
    current_user_full_name: str,
    top_n: int = DEFAULT_TOP_N,
) -> list[ScoredTopic]:
    """
    Given a flat list of message dicts (as returned by messages_for_ids),
    group them by (stream_id, topic), score each group, and return the
    top_n topics sorted by descending score.

    Each message dict is expected to have at minimum:
        sender_full_name, stream_id, display_recipient (stream name),
        subject (topic), id, timestamp, reactions (list), content
    """
    now = time.time()

    # ── Group messages by (stream_id, topic) ─────────────────────────────────
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for msg in messages:
        stream_id   = msg.get("stream_id", 0)
        topic       = msg.get("subject", "")
        key         = (stream_id, topic)
        groups.setdefault(key, []).append(msg)

    scored: list[ScoredTopic] = []

    for (stream_id, topic), msgs in groups.items():
        # Stream name
        recipient = msgs[0].get("display_recipient", "")
        stream_name = recipient if isinstance(recipient, str) else recipient[0].get("name", "")

        # Sort messages by id for consistent first/last
        msgs_sorted = sorted(msgs, key=lambda m: m["id"])
        first_msg   = msgs_sorted[0]
        latest_msg  = msgs_sorted[-1]

        # ── Scoring factors ───────────────────────────────────────────────────
        mention_pattern = re.compile(
            r"@\*\*" + re.escape(current_user_full_name) + r"\*\*",
            re.IGNORECASE,
        )

        has_mention          = False
        has_wildcard_mention = False
        total_reactions      = 0
        senders: set[str]    = set()

        for msg in msgs:
            content = msg.get("content", "")
            if mention_pattern.search(content):
                has_mention = True
            if _WILDCARD_MENTION.search(content):
                has_wildcard_mention = True
            senders.add(msg.get("sender_full_name", ""))
            reactions = msg.get("reactions", [])
            total_reactions += len(reactions) if isinstance(reactions, list) else 0

        message_count   = len(msgs)
        sender_count    = len(senders)

        # Recency: how recent is the latest message (normalised 0–1 over 7 days)
        latest_ts       = latest_msg.get("timestamp", now)
        age_hours       = max(0.0, (now - latest_ts) / 3600)
        recency_score   = max(0.0, 1.0 - age_hours / (7 * 24))

        # Reply count factor: log-scaled
        import math
        reply_factor    = math.log1p(message_count)

        # Sender diversity factor: log-scaled
        diversity_factor = math.log1p(sender_count)

        # Reaction factor: log-scaled
        reaction_factor = math.log1p(total_reactions)

        score = (
            (W_DIRECT_MENTION   * (1.0 if has_mention else 0.0))
            + (W_WILDCARD_MENTION * (1.0 if has_wildcard_mention else 0.0))
            + (W_SENDER_DIVERSITY * diversity_factor)
            + (W_REPLY_COUNT      * reply_factor)
            + (W_REACTIONS        * reaction_factor)
            + (W_RECENCY          * recency_score)
        )

        # ── Sample messages (first 3 per topic, plain text) ───────────────────
        sample_messages = [
            {
                "sender": m.get("sender_full_name", ""),
                "content": m.get("content", "")[:200],
                "id": m["id"],
                "timestamp": m.get("timestamp", 0),
            }
            for m in msgs_sorted[:3]
        ]

        scored.append(
            ScoredTopic(
                stream_id       = stream_id,
                stream_name     = stream_name,
                topic           = topic,
                score           = round(score, 3),
                message_count   = message_count,
                sender_count    = sender_count,
                first_message_id = first_msg["id"],
                latest_message_id = latest_msg["id"],
                narrow_url      = _narrow_url(stream_id, stream_name, topic),
                sample_messages = sample_messages,
                has_mention     = has_mention,
                has_wildcard_mention = has_wildcard_mention,
                reaction_count  = total_reactions,
            )
        )

    # Sort by score descending, return top N
    scored.sort(key=lambda t: t.score, reverse=True)
    return scored[:top_n]

"""
GET /json/catch-up/summary

Returns a Claude-generated structured summary of all messages the user missed,
with per-action-item and per-topic deep-link context references (US-08).

Response:
{
  "overview":     "<plain-text overview>",
  "keywords":     ["kw1", ...],
  "action_items": [{"text", "assignee", "message_id", "narrow_url"}, ...],
  "topics":       [{"stream", "topic", "summary", "narrow_url",
                    "key_messages": [{"id", "excerpt", "narrow_url"}, ...]}, ...],
  "model_used":   "anthropic/claude-...",
  "message_count": N
}
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from zerver.lib.exceptions import JsonableError
from zerver.lib.message import messages_for_ids
from zerver.lib.narrow import (
    LARGER_THAN_MAX_MESSAGE_ID,
    AnchorInfo,
    fetch_messages,
)
from zerver.lib.response import json_success
from zerver.lib.typed_endpoint import typed_endpoint_without_parameters
from zerver.models import UserProfile
from zerver.models.realms import MessageEditHistoryVisibilityPolicyEnum

MAX_CATCH_UP_SUMMARY_MESSAGES = 200


@typed_endpoint_without_parameters
def get_catch_up_summary(
    request: HttpRequest,
    user_profile: UserProfile,
) -> HttpResponse:
    """
    Generate a Claude-powered structured summary of all unread messages.
    Falls back to the local NLP pipeline if no LLM model is configured.
    """
    model = settings.TOPIC_SUMMARIZATION_MODEL
    api_key = settings.TOPIC_SUMMARIZATION_API_KEY

    # Fetch all unread messages (no narrow = whole inbox)
    query_info = fetch_messages(
        narrow=None,
        user_profile=user_profile,
        realm=user_profile.realm,
        is_web_public_query=False,
        anchor_info=AnchorInfo(type="first_unread", value=None),
        include_anchor=True,
        num_before=0,
        num_after=MAX_CATCH_UP_SUMMARY_MESSAGES,
    )

    if not query_info.rows:
        raise JsonableError("No unread messages to summarise.")

    result_message_ids: list[int] = [row[0] for row in query_info.rows]
    user_message_flags: dict[int, list[str]] = {mid: [] for mid in result_message_ids}

    message_list = messages_for_ids(
        message_ids=result_message_ids,
        user_message_flags=user_message_flags,
        search_fields={},
        apply_markdown=False,
        client_gravatar=True,
        allow_empty_topic_name=True,
        message_edit_history_visibility_policy=MessageEditHistoryVisibilityPolicyEnum.none.value,
        user_profile=user_profile,
        realm=user_profile.realm,
    )

    if not message_list:
        raise JsonableError("No messages found to summarise.")

    # ── Claude path ────────────────────────────────────────────────────────────
    if model is not None:
        from zerver.lib.catchup_claude import CatchUpSummary, summarize_with_claude, summary_to_dict

        extra: dict[str, object] = dict(settings.TOPIC_SUMMARIZATION_PARAMETERS)
        try:
            summary: CatchUpSummary = summarize_with_claude(
                messages=message_list,
                model=model,
                api_key=api_key,
                extra_params=extra,
            )
        except Exception as e:
            # Surface the error clearly so the frontend can show a useful message
            raise JsonableError(f"Claude API error: {e}") from e

        return json_success(request, {"structured": True, **summary_to_dict(summary)})

    # ── Local NLP fallback ─────────────────────────────────────────────────────
    from zerver.lib.catchup_nlp import summarize_topic

    result = summarize_topic(message_list)
    conf_pct = int(result.confidence * 100)

    action_items_plain = [
        {
            "text": item.text,
            "assignee": item.source_sender or None,
            "message_id": None,
            "narrow_url": None,
        }
        for item in result.action_items
    ]

    return json_success(
        request,
        {
            "structured": False,
            "overview": result.summary,
            "keywords": result.keywords,
            "action_items": action_items_plain,
            "topics": [],
            "model_used": f"local-nlp/{result.backend}",
            "message_count": result.message_count,
            "confidence": conf_pct,
        },
    )

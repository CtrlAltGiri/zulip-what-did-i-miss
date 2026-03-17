"""
Catch-up API endpoint.

GET /json/catch-up
  Returns missed messages grouped by topic, scored by importance (US-07),
  with deep-link context URLs (US-08).
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from zerver.lib.message import messages_for_ids
from zerver.lib.narrow import (
    AnchorInfo,
    fetch_messages,
)
from zerver.lib.response import json_success
from zerver.lib.typed_endpoint import typed_endpoint_without_parameters
from zerver.models import UserProfile
from zerver.models.realms import MessageEditHistoryVisibilityPolicyEnum

MAX_CATCH_UP_MESSAGES = 500


@typed_endpoint_without_parameters
def get_catch_up_data(
    request: HttpRequest,
    user_profile: UserProfile,
) -> HttpResponse:
    """
    Returns missed messages grouped and scored for the catch-up view.
    Topics are ranked by importance (US-07) and include deep-link URLs (US-08).
    """
    # Fetch recent messages across all subscribed streams (no narrow = combined feed)
    query_info = fetch_messages(
        narrow=None,
        user_profile=user_profile,
        realm=user_profile.realm,
        is_web_public_query=False,
        anchor_info=AnchorInfo(type="first_unread", value=None),
        include_anchor=True,
        num_before=0,
        num_after=MAX_CATCH_UP_MESSAGES,
    )

    if not query_info.rows:
        return json_success(request, {
            "topics": [],
            "total_messages": 0,
        })

    result_message_ids: list[int] = []
    user_message_flags: dict[int, list[str]] = {}
    for row in query_info.rows:
        message_id = row[0]
        result_message_ids.append(message_id)
        user_message_flags[message_id] = []

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
        return json_success(request, {
            "topics": [],
            "total_messages": 0,
        })

    # Score and rank topics (US-07 + US-08)
    from zerver.lib.catchup_importance import score_topics  # avoid circular at module level

    scored_topics = score_topics(
        messages=message_list,
        current_user_full_name=user_profile.full_name,
    )

    topics_data = [
        {
            "stream_id":        t.stream_id,
            "stream_name":      t.stream_name,
            "topic":            t.topic,
            "score":            t.score,
            "message_count":    t.message_count,
            "sender_count":     t.sender_count,
            "first_message_id": t.first_message_id,
            "latest_message_id": t.latest_message_id,
            "narrow_url":       t.narrow_url,
            "sample_messages":  t.sample_messages,
            "has_mention":      t.has_mention,
            "has_wildcard_mention": t.has_wildcard_mention,
            "reaction_count":   t.reaction_count,
        }
        for t in scored_topics
    ]

    return json_success(request, {
        "topics": topics_data,
        "total_messages": len(message_list),
    })

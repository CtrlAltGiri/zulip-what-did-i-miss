/**
 * Catch-up view — "What did I miss?"
 *
 * Tabs:
 *   All          — all missed topics, sorted by importance score (US-07)
 *   @Mentions    — topics where the user was @-mentioned
 *   ★ Important  — high-score or mentioned topics
 *
 * AI Summary button — inline dismissible NLP summary panel
 */

import $ from "jquery";

import * as channel from "./channel.ts";
import {$t} from "./i18n.ts";
import * as left_sidebar_navigation_area from "./left_sidebar_navigation_area.ts";

let is_visible = false;
let active_tab: "all" | "mentions" | "important" = "all";
let summary_panel_open = false;
// cached_summary kept for type compatibility but superseded by cached_claude_response

type SampleMessage = {sender: string; content: string; id: number; timestamp: number};

type TopicData = {
    stream_id: number;
    stream_name: string;
    topic: string;
    score: number;
    message_count: number;
    sender_count: number;
    first_message_id: number;
    latest_message_id: number;
    narrow_url: string;
    sample_messages: SampleMessage[];
    has_mention: boolean;
    has_wildcard_mention: boolean;
    reaction_count: number;
};

let cached_topics: TopicData[] = [];
let total_messages = 0;
let cached_summary = "";
let is_demo_mode = false;

// ── Demo / Simulation data (used when API returns no unread messages) ──────────

const _NOW = Math.floor(Date.now() / 1000);

const DEMO_TOPICS: TopicData[] = [
    {
        stream_id: 4,
        stream_name: "devel",
        topic: "Sprint 2 planning",
        score: 8.5,
        message_count: 4,
        sender_count: 3,
        first_message_id: 1001,
        latest_message_id: 1004,
        narrow_url: "#narrow/channel/4-devel/topic/Sprint.2.planning",
        has_mention: true,
        has_wildcard_mention: false,
        reaction_count: 2,
        sample_messages: [
            {id: 1001, sender: "Giridhar", content: "We discussed the new authentication flow and decided to use OAuth2 for the catch-up feature.", timestamp: _NOW - 3500},
            {id: 1002, sender: "Dennis", content: "TODO: Update the CI/CD pipeline to run NLP tests on every PR.", timestamp: _NOW - 3200},
            {id: 1003, sender: "Sanjeev", content: "@**You** Please review the PR before the standup. Frontend wireframes are ready.", timestamp: _NOW - 2900},
        ],
    },
    {
        stream_id: 4,
        stream_name: "devel",
        topic: "NLP pipeline review",
        score: 7.0,
        message_count: 3,
        sender_count: 2,
        first_message_id: 1005,
        latest_message_id: 1007,
        narrow_url: "#narrow/channel/4-devel/topic/NLP.pipeline.review",
        has_mention: true,
        has_wildcard_mention: false,
        reaction_count: 1,
        sample_messages: [
            {id: 1005, sender: "Swathi", content: "The extractive summarization pipeline is working well. Confidence scores above 70% on test data.", timestamp: _NOW - 2800},
            {id: 1006, sender: "Giridhar", content: "@**You** can you validate the keyword extraction results? Need your sign-off.", timestamp: _NOW - 2600},
            {id: 1007, sender: "Swathi", content: "Also need to wire up action item detection to the catch-up endpoint.", timestamp: _NOW - 2400},
        ],
    },
    {
        stream_id: 5,
        stream_name: "test",
        topic: "Deploy schedule",
        score: 5.5,
        message_count: 3,
        sender_count: 2,
        first_message_id: 1008,
        latest_message_id: 1010,
        narrow_url: "#narrow/channel/5-test/topic/Deploy.schedule",
        has_mention: false,
        has_wildcard_mention: true,
        reaction_count: 0,
        sample_messages: [
            {id: 1008, sender: "Dennis", content: "@**all** Hotfix deployment scheduled for 5 PM today. Please wrap up your work by then.", timestamp: _NOW - 2200},
            {id: 1009, sender: "Giridhar", content: "I'll deploy the hotfix to staging by EOD today and monitor the deployment.", timestamp: _NOW - 2000},
            {id: 1010, sender: "Sanjeev", content: "Confirmed. Staging tests passed. Ready for prod.", timestamp: _NOW - 1800},
        ],
    },
    {
        stream_id: 4,
        stream_name: "devel",
        topic: "Auth middleware refactor",
        score: 4.2,
        message_count: 2,
        sender_count: 2,
        first_message_id: 1011,
        latest_message_id: 1012,
        narrow_url: "#narrow/channel/4-devel/topic/Auth.middleware.refactor",
        has_mention: true,
        has_wildcard_mention: false,
        reaction_count: 1,
        sample_messages: [
            {id: 1011, sender: "Giridhar", content: "Refactored session token storage to meet the new compliance requirements.", timestamp: _NOW - 1600},
            {id: 1012, sender: "Dennis", content: "@**You** This needs a security review. Can you take a look before EOD?", timestamp: _NOW - 1400},
        ],
    },
    {
        stream_id: 6,
        stream_name: "social",
        topic: "Team lunch Friday",
        score: 1.8,
        message_count: 3,
        sender_count: 3,
        first_message_id: 1013,
        latest_message_id: 1015,
        narrow_url: "#narrow/channel/6-social/topic/Team.lunch.Friday",
        has_mention: false,
        has_wildcard_mention: false,
        reaction_count: 5,
        sample_messages: [
            {id: 1013, sender: "Zoe", content: "Team lunch on Friday at 12:30! Pizza place on Main St.", timestamp: _NOW - 1200},
            {id: 1014, sender: "Cordelia", content: "Count me in! 🍕", timestamp: _NOW - 1100},
            {id: 1015, sender: "aaron", content: "Same, see you all there!", timestamp: _NOW - 1000},
        ],
    },
];

const DEMO_SUMMARY = `You missed 12 messages across 5 topics over the last hour.

Key discussion: The team agreed to use OAuth2 for the catch-up feature authentication. The NLP pipeline extractive summarization is performing well with confidence above 70%.

Keywords: authentication, OAuth2, NLP pipeline, CI/CD, hotfix, deploy

Action items
• Update the CI/CD pipeline to run NLP tests on every PR — Dennis
• Review the PR before the standup (wireframes ready) — You
• Validate keyword extraction results — You
• Security review of auth middleware refactor — You
• Deploy hotfix to staging by EOD today — Giridhar

Confidence 72% · 12 messages · 5/15 sentences · backend: frequency`;

// ── Utilities ─────────────────────────────────────────────────────────────────

function format_timestamp(ts: number): string {
    if (!ts) {
        return "";
    }
    const now = Date.now();
    const msg = new Date(ts * 1000);
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);

    const time = msg.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
    if (msg.toDateString() === today.toDateString()) {
        return `Today ${time}`;
    }
    if (msg.toDateString() === yesterday.toDateString()) {
        return `Yesterday ${time}`;
    }
    const diff_days = Math.floor((now - ts * 1000) / 86_400_000);
    if (diff_days < 7) {
        return `${diff_days}d ago ${time}`;
    }
    return msg.toLocaleDateString([], {month: "short", day: "numeric"}) + " " + time;
}

function time_away_label(topics: TopicData[]): string {
    let oldest = Infinity;
    for (const t of topics) {
        for (const m of t.sample_messages) {
            if (m.timestamp && m.timestamp < oldest) {
                oldest = m.timestamp;
            }
        }
    }
    if (oldest === Infinity) {
        return "";
    }
    const hours = Math.round((Date.now() / 1000 - oldest) / 3600);
    if (hours < 1) {
        return "less than an hour away";
    }
    if (hours < 24) {
        return `${hours} hour${hours === 1 ? "" : "s"} away`;
    }
    const days = Math.round(hours / 24);
    return `${days} day${days === 1 ? "" : "s"} away`;
}

function avatar_color(name: string): string {
    const palette = [
        "#e03997", "#7c5cfc", "#3d9df5", "#22b8cf", "#20c997",
        "#94d82d", "#fcc419", "#ff922b", "#f03e3e", "#74c0fc",
    ];
    let h = 0;
    for (let i = 0; i < name.length; i++) {
        h = ((h * 31) + name.charCodeAt(i)) & 0xffff;
    }
    return palette[h % palette.length]!;
}

// ── Show / hide ───────────────────────────────────────────────────────────────

export function show(): void {
    if (is_visible) {
        return;
    }
    is_visible = true;
    active_tab = "all";
    summary_panel_open = false;
    cached_summary = "";

    // Overlay the entire column-middle using absolute positioning.
    // This avoids fighting with existing Zulip headers that may still render.
    const $middle = $(".app-main .column-middle");
    $middle.css("position", "relative");
    $("#catch-up-view").remove();

    const $view = $(build_view_html());
    $middle.append($view);

    left_sidebar_navigation_area.select_top_left_corner_item(".top_left_catch_up");

    // Tab clicks
    $(document).on("click.catch-up", ".cu-tab", function () {
        const tab = $(this).data("tab") as typeof active_tab;
        if (tab !== active_tab) {
            set_active_tab(tab);
        }
    });

    // AI Summary button
    $(document).on("click.catch-up", "#cu-ai-btn", () => {
        toggle_summary_panel();
    });

    // Context links inside the catch-up view (narrow links from summary / topic cards)
    // Hide the catch-up view first, then let Zulip's normal hash routing take over.
    $(document).on("click.catch-up", "#catch-up-view a[href^='#narrow']", function (e) {
        e.preventDefault();
        const href = $(this).attr("href");
        if (!href) {
            return;
        }
        hide();
        // Use setTimeout so hide() finishes restoring the DOM before hash change fires
        setTimeout(() => {
            window.location.hash = href;
        }, 50);
    });

    load_topics();
}

export function hide(): void {
    if (!is_visible) {
        return;
    }
    is_visible = false;
    is_demo_mode = false;
    $(document).off("click.catch-up");
    $("#catch-up-view").remove();
    $(".app-main .column-middle").css("position", "");
}

export function get_is_visible(): boolean {
    return is_visible;
}

// ── HTML builder ──────────────────────────────────────────────────────────────

function build_view_html(): string {
    return `
        <div id="catch-up-view" style="
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            z-index: 200;
            display: flex;
            flex-direction: column;
            background: var(--color-background, #fff);
            overflow: hidden;
        ">
            <!-- ── Header ── -->
            <div style="
                padding: 10px 20px 0;
                border-bottom: 1px solid var(--color-border-sidebar, #e0e4ea);
                flex-shrink: 0;
            ">
                <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:2px;">
                    <div>
                        <h1 style="
                            font-size: 20px;
                            font-weight: 700;
                            margin: 0 0 1px;
                            color: var(--color-text-default);
                        ">${$t({defaultMessage: "What did I miss?"})}</h1>
                        <div id="cu-away" style="font-size:12px; color:#888;"></div>
                    </div>
                    <button id="cu-ai-btn" style="
                        display: inline-flex;
                        align-items: center;
                        gap: 6px;
                        padding: 8px 18px;
                        border-radius: 22px;
                        background: #1c3a5e;
                        color: #fff;
                        font-size: 13px;
                        font-weight: 700;
                        border: none;
                        cursor: pointer;
                        white-space: nowrap;
                        flex-shrink: 0;
                        box-shadow: 0 2px 8px rgba(28,58,94,0.3);
                    ">✦ ${$t({defaultMessage: "AI Summary"})}</button>
                </div>

                <!-- Stats row -->
                <div id="cu-stats" style="
                    font-size: 13px;
                    color: #888;
                    margin: 4px 0 10px;
                    display: flex;
                    gap: 16px;
                    align-items: center;
                ">
                    <span>${$t({defaultMessage: "Loading…"})}</span>
                </div>

                <!-- Tabs -->
                <div style="display:flex; gap:0;">
                    <button class="cu-tab" data-tab="all" style="${tab_style(true)}">
                        ${$t({defaultMessage: "All"})}
                    </button>
                    <button class="cu-tab" data-tab="mentions" style="${tab_style(false)}">
                        @ ${$t({defaultMessage: "Mentions"})}
                    </button>
                    <button class="cu-tab" data-tab="important" style="${tab_style(false)}">
                        ★ ${$t({defaultMessage: "Important"})}
                    </button>
                </div>
            </div>

            <!-- ── Body ── -->
            <div id="cu-body" style="
                flex: 1;
                overflow-y: auto;
                padding: 12px 20px 24px;
            ">
                <div style="text-align:center; padding:48px 0; color:#888; font-size:14px;">
                    ${$t({defaultMessage: "Loading missed messages…"})}
                </div>
            </div>
        </div>
    `;
}

// ── Tab management ────────────────────────────────────────────────────────────

function tab_style(active: boolean): string {
    const base = `
        padding: 9px 18px;
        border: none;
        border-radius: 0;
        background: none;
        cursor: pointer;
        font-size: 14px;
        font-weight: 600;
        border-bottom: 3px solid transparent;
        transition: color 0.12s, border-color 0.12s;
        outline: none;
    `;
    return active
        ? base + "color: hsl(218,57%,38%); border-bottom-color: hsl(218,57%,38%);"
        : base + "color: #888;";
}

function set_active_tab(tab: typeof active_tab): void {
    active_tab = tab;
    $(".cu-tab").each(function () {
        const is_active = $(this).data("tab") === tab;
        $(this).attr("style", tab_style(is_active));
    });
    render_topics();
}

// ── AI Summary panel (Claude-powered) ────────────────────────────────────────

type ClaudeSummaryResponse = {
    structured: boolean;
    overview: string;
    keywords: string[];
    action_items: {text: string; assignee: string | null; message_id: number | null; narrow_url: string | null}[];
    topics: {
        stream: string;
        topic: string;
        summary: string;
        narrow_url: string;
        key_messages: {id: number; excerpt: string; narrow_url: string}[];
    }[];
    model_used: string;
    message_count: number;
    confidence?: number;
};

let cached_claude_response: ClaudeSummaryResponse | null = null;

function toggle_summary_panel(): void {
    summary_panel_open = !summary_panel_open;
    if (!summary_panel_open) {
        $("#cu-summary-panel").slideUp(150, function () {
            $(this).remove();
        });
        return;
    }

    if (cached_claude_response) {
        show_summary_panel(cached_claude_response);
        return;
    }

    open_loading_panel();
    fetch_claude_summary();
}

function open_loading_panel(): void {
    $("#cu-summary-panel").remove();
    const $panel = build_panel_shell(
        `<div style="color:#888; font-style:italic; font-size:14px; padding:18px 20px;">
            ✦ ${$t({defaultMessage: "Claude is analysing your missed messages…"})}
         </div>`,
    );
    $("#cu-body").prepend($panel);
    $panel.hide().slideDown(160);
}

function build_panel_shell(inner_html: string): JQuery {
    const $panel = $(`
        <div id="cu-summary-panel" style="
            border: 1px solid #c8d6e0;
            border-radius: 10px;
            overflow: hidden;
            margin-bottom: 18px;
            background: #fff;
            box-shadow: 0 2px 12px rgba(0,0,0,0.08);
        ">
            <div style="
                background: #1c3a5e; color: #fff;
                padding: 12px 18px; font-size: 14px; font-weight: 700;
                display: flex; align-items: center; justify-content: space-between;
            ">
                <span>✦ ${$t({defaultMessage: "AI Summary of Missed Messages"})}</span>
                <button id="cu-panel-close" style="
                    background:none; border:none; color:#fff;
                    font-size:20px; cursor:pointer; line-height:1; padding:0 2px; opacity:0.8;
                ">×</button>
            </div>
            <div id="cu-summary-body" style="color:#1a1a1a; background:#fff;">
                ${inner_html}
            </div>
        </div>
    `);
    $panel.find("#cu-panel-close").on("click", () => {
        summary_panel_open = false;
        $panel.slideUp(150, () => $panel.remove());
    });
    return $panel;
}

function show_summary_panel(data: ClaudeSummaryResponse): void {
    $("#cu-summary-panel").remove();
    const $panel = build_panel_shell(render_claude_summary(data));
    $("#cu-body").prepend($panel);
    $panel.hide().slideDown(160);
}

function fetch_claude_summary(): void {
    void channel.get({
        url: "/json/catch-up/summary",
        success(raw: unknown) {
            const data = raw as ClaudeSummaryResponse;
            cached_claude_response = data;
            if (summary_panel_open) {
                show_summary_panel(data);
            }
        },
        error(xhr: {responseJSON?: {msg?: string}}) {
            const msg = xhr.responseJSON?.msg ?? $t({defaultMessage: "Failed to generate summary."});
            // Fall back to demo structured data so UI is still demonstrable
            cached_claude_response = build_demo_claude_response();
            if (summary_panel_open) {
                show_summary_panel(cached_claude_response);
            }
            void msg; // suppress unused warning — error shown via demo fallback
        },
    });
}

function build_demo_claude_response(): ClaudeSummaryResponse {
    return {
        structured: true,
        overview: DEMO_SUMMARY.split("\n")[0]!,
        keywords: ["authentication", "OAuth2", "NLP pipeline", "CI/CD", "hotfix", "deploy"],
        action_items: [
            {text: "Update the CI/CD pipeline to run NLP tests on every PR", assignee: "Dennis", message_id: null, narrow_url: null},
            {text: "Review the PR before the standup — wireframes ready", assignee: "You", message_id: null, narrow_url: null},
            {text: "Validate keyword extraction results and give sign-off", assignee: "You", message_id: null, narrow_url: null},
            {text: "Security review of auth middleware refactor", assignee: "You", message_id: null, narrow_url: null},
            {text: "Deploy hotfix to staging by EOD today", assignee: "Giridhar", message_id: null, narrow_url: null},
        ],
        topics: [
            {stream: "devel", topic: "Sprint 2 planning", summary: "Team agreed to use OAuth2 for authentication. PR review and CI/CD updates were requested.", narrow_url: "", key_messages: [{id: 1002, excerpt: "TODO: Update the CI/CD pipeline to run NLP tests", narrow_url: ""}, {id: 1003, excerpt: "Please review the PR before the standup", narrow_url: ""}]},
            {stream: "devel", topic: "NLP pipeline review", summary: "Extractive summarization is working well with 70%+ confidence. Sign-off needed on keyword extraction.", narrow_url: "", key_messages: [{id: 1005, excerpt: "Confidence scores above 70% on test data", narrow_url: ""}, {id: 1006, excerpt: "Can you validate the keyword extraction results?", narrow_url: ""}]},
            {stream: "test", topic: "Deploy schedule", summary: "@all: hotfix deployment at 5 PM today. Staging tests passed.", narrow_url: "", key_messages: [{id: 1008, excerpt: "Hotfix deployment scheduled for 5 PM today", narrow_url: ""}]},
        ],
        model_used: "demo",
        message_count: 12,
    };
}

function link(url: string, label: string): string {
    return `<a href="${url}" style="color:hsl(218,57%,38%); font-weight:600; text-decoration:none; font-size:12px; white-space:nowrap;">${label} ↗</a>`;
}

function esc(s: string): string {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function render_claude_summary(data: ClaudeSummaryResponse): string {
    let html = `<div style="padding:18px 20px;">`;

    // ── Overview ──────────────────────────────────────────────────────────────
    html += `
        <div style="font-size:14px; line-height:1.7; color:#222; margin-bottom:14px;">
            ${esc(data.overview)}
        </div>
    `;

    // ── Keywords ──────────────────────────────────────────────────────────────
    if (data.keywords.length > 0) {
        const tags = data.keywords
            .map(
                (k) =>
                    `<span style="background:#eef2ff; color:#3451b2; border-radius:4px;
                        padding:2px 8px; font-size:12px; font-weight:600; display:inline-block;
                        margin:2px 4px 2px 0;">${esc(k)}</span>`,
            )
            .join("");
        html += `
            <div style="margin-bottom:16px;">
                <div style="font-size:12px; font-weight:700; color:#888; text-transform:uppercase;
                    letter-spacing:0.05em; margin-bottom:6px;">Keywords</div>
                <div>${tags}</div>
            </div>
        `;
    }

    // ── Action items (each linked to its source message) ─────────────────────
    if (data.action_items.length > 0) {
        html += `
            <div style="margin-bottom:16px;">
                <div style="font-size:12px; font-weight:700; color:#888; text-transform:uppercase;
                    letter-spacing:0.05em; margin-bottom:8px;">Action Items</div>
                <div style="display:flex; flex-direction:column; gap:6px;">
        `;
        for (const item of data.action_items) {
            const assignee_badge = item.assignee
                ? `<span style="background:#fef3c7; color:#92400e; border-radius:4px;
                    padding:1px 7px; font-size:11px; font-weight:700; flex-shrink:0;">${esc(item.assignee)}</span>`
                : "";
            const src_link = item.narrow_url
                ? `<span style="flex-shrink:0;">${link(item.narrow_url, "View source")}</span>`
                : "";
            html += `
                <div style="display:flex; align-items:flex-start; gap:8px;
                    background:#f8faff; border:1px solid #e0e7ff; border-radius:6px; padding:8px 12px;">
                    <span style="color:#3451b2; flex-shrink:0; margin-top:1px;">◆</span>
                    <span style="flex:1; font-size:13.5px; color:#222; line-height:1.5;">${esc(item.text)}</span>
                    ${assignee_badge}
                    ${src_link}
                </div>
            `;
        }
        html += `</div></div>`;
    }

    // ── Per-topic summaries with key message links ────────────────────────────
    if (data.topics.length > 0) {
        html += `
            <div>
                <div style="font-size:12px; font-weight:700; color:#888; text-transform:uppercase;
                    letter-spacing:0.05em; margin-bottom:8px;">Topics</div>
                <div style="display:flex; flex-direction:column; gap:8px;">
        `;
        for (const t of data.topics) {
            const key_msg_links = t.key_messages
                .map(
                    (km) =>
                        `<div style="display:flex; align-items:flex-start; gap:6px;
                            padding:5px 10px; background:#fff; border:1px solid #e8edf2;
                            border-radius:5px; margin-top:4px;">
                            <span style="color:#aaa; font-size:11px; flex-shrink:0; margin-top:1px;">↳</span>
                            <span style="flex:1; font-size:12.5px; color:#444; line-height:1.5;">${esc(km.excerpt)}</span>
                            ${km.narrow_url ? link(km.narrow_url, "Jump") : ""}
                        </div>`,
                )
                .join("");

            html += `
                <div style="border:1px solid #e0e4ea; border-radius:8px; overflow:hidden;">
                    <div style="display:flex; align-items:center; gap:8px; padding:8px 12px;
                        background:#f5f7fa; border-bottom:1px solid #e8edf2;">
                        <span style="background:#1c3a5e; color:#fff; border-radius:4px;
                            padding:2px 8px; font-size:11px; font-weight:700;">#${esc(t.stream)}</span>
                        <span style="font-size:13px; font-weight:600; flex:1; color:#222;">${esc(t.topic)}</span>
                        ${t.narrow_url ? link(t.narrow_url, "View thread") : ""}
                    </div>
                    <div style="padding:8px 12px;">
                        <div style="font-size:13px; color:#444; line-height:1.6; margin-bottom:4px;">${esc(t.summary)}</div>
                        ${key_msg_links}
                    </div>
                </div>
            `;
        }
        html += `</div></div>`;
    }

    // ── Footer ────────────────────────────────────────────────────────────────
    const model_label = data.model_used === "demo" ? "demo data" : data.model_used;
    html += `
        <div style="margin-top:14px; padding-top:10px; border-top:1px solid #f0f2f5;
            font-size:11px; color:#aaa; display:flex; justify-content:space-between;">
            <span>${data.message_count} messages analysed</span>
            <span>model: ${esc(model_label)}</span>
        </div>
    `;

    html += `</div>`;
    return html;
}

// ── Load topics ───────────────────────────────────────────────────────────────

function load_topics(): void {
    void channel.get({
        url: "/json/catch-up",
        success(raw: unknown) {
            const data = raw as {topics: TopicData[]; total_messages: number};
            const topics = data.topics ?? [];
            const total = data.total_messages ?? 0;

            if (topics.length === 0) {
                // No real unread data — use demo simulation
                apply_data(DEMO_TOPICS, DEMO_TOPICS.reduce((s, t) => s + t.message_count, 0), true);
            } else {
                apply_data(topics, total, false);
            }
        },
        error() {
            // API error — still show demo data so UI is testable
            apply_data(DEMO_TOPICS, DEMO_TOPICS.reduce((s, t) => s + t.message_count, 0), true);
        },
    });
}

function apply_data(topics: TopicData[], total: number, is_demo: boolean): void {
    cached_topics = topics;
    total_messages = total;
    is_demo_mode = is_demo;

    const topic_count = topics.length;
    const mention_count = topics.filter((t) => t.has_mention).length;

    $("#cu-away").text(time_away_label(topics) + (is_demo ? " (demo)" : ""));
    $("#cu-stats").html(`
        <span style="display:flex; gap:6px; align-items:center;">
            <span style="font-size:15px;">💬</span> <strong>${total}</strong> messages
        </span>
        <span style="color:#ccc;">·</span>
        <span style="display:flex; gap:6px; align-items:center;">
            <span style="font-size:15px;">#</span> <strong>${topic_count}</strong> topics
        </span>
        ${mention_count > 0 ? `<span style="color:#ccc;">·</span>
        <span style="color:#ef4444; font-weight:600;">@${mention_count} mentions</span>` : ""}
    `);

    if (mention_count > 0) {
        $(`.cu-tab[data-tab="mentions"]`).text(`@ Mentions (${mention_count})`);
    }

    render_topics();
}

// ── Render topics ─────────────────────────────────────────────────────────────

function render_topics(): void {
    const $body = $("#cu-body");

    // Preserve summary panel
    const $panel = $("#cu-summary-panel").detach();

    const topics =
        active_tab === "mentions"
            ? cached_topics.filter((t) => t.has_mention || t.has_wildcard_mention)
            : active_tab === "important"
              ? cached_topics.filter((t) => t.has_mention || t.score >= 4)
              : cached_topics;

    $body.empty();
    if ($panel.length) {
        $body.append($panel);
    }

    if (topics.length === 0) {
        const msg =
            active_tab === "mentions"
                ? $t({defaultMessage: "No mentions while you were away."})
                : $t({defaultMessage: "No important topics found."});
        $body.append(`
            <div style="text-align:center; padding:64px 0; color:#aaa; font-size:15px;">
                <div style="font-size:40px; margin-bottom:12px;">✓</div>
                ${msg}
            </div>
        `);
        return;
    }

    for (const topic of topics) {
        $body.append(render_card(topic));
    }
}

function render_card(topic: TopicData): JQuery {
    const stream_badge = `
        <span style="
            background: #1c3a5e; color: #fff;
            border-radius: 4px; padding: 2px 8px;
            font-size: 11px; font-weight: 700;
            margin-right: 8px; flex-shrink:0;
        ">#${topic.stream_name}</span>
    `;

    const mention_badge = topic.has_mention
        ? `<span style="background:#ef4444; color:#fff; border-radius:4px;
            padding:2px 8px; font-size:11px; font-weight:700; flex-shrink:0;">@you</span>`
        : topic.has_wildcard_mention
          ? `<span style="background:#f59e0b; color:#fff; border-radius:4px;
                padding:2px 8px; font-size:11px; font-weight:700; flex-shrink:0;">@all</span>`
          : "";

    const msgs_html = topic.sample_messages
        .map(
            (m) => `
            <div style="
                display: flex; gap: 10px;
                padding: 10px 16px;
                border-top: 1px solid var(--color-border-sidebar, #f0f2f5);
            ">
                <div style="
                    width: 34px; height: 34px; border-radius: 50%;
                    background: ${avatar_color(m.sender)};
                    color: #fff; flex-shrink: 0;
                    display: flex; align-items: center; justify-content: center;
                    font-size: 13px; font-weight: 700;
                ">${m.sender ? m.sender[0]!.toUpperCase() : "?"}</div>
                <div style="flex:1; min-width:0;">
                    <div style="display:flex; align-items:baseline; gap:8px; margin-bottom:3px;">
                        <span style="font-weight:700; font-size:13px; color:var(--color-text-default);">${m.sender}</span>
                        <span style="font-size:11px; color:#aaa;">${format_timestamp(m.timestamp)}</span>
                    </div>
                    <div style="font-size:13.5px; color:var(--color-text-default); line-height:1.5;">
                        ${m.content}
                    </div>
                </div>
            </div>
        `,
        )
        .join("");

    const open_link = is_demo_mode
        ? `<span style="font-size:12px; color:#bbb; font-style:italic;">demo data</span>`
        : `<a href="${topic.narrow_url}" style="
                font-size: 12px; color: hsl(218,57%,38%);
                font-weight: 600; text-decoration: none;
            ">Open conversation ↗</a>`;

    const footer = `
        <div style="
            padding: 7px 16px; font-size: 12px; color: #aaa;
            border-top: 1px solid var(--color-border-sidebar, #f0f2f5);
            display: flex; justify-content: space-between; align-items: center;
        ">
            <span>${topic.message_count} message${topic.message_count === 1 ? "" : "s"} · ${topic.sender_count} sender${topic.sender_count === 1 ? "" : "s"}</span>
            ${open_link}
        </div>
    `;

    return $(`
        <div style="
            margin-bottom: 14px;
            border: 1px solid var(--color-border-sidebar, #e0e4ea);
            border-radius: 10px;
            overflow: hidden;
            background: var(--color-background, #fff);
        ">
            <!-- Card header -->
            <div style="
                display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
                padding: 10px 16px;
                background: var(--color-background-sidebar, #f5f7fa);
                border-bottom: 1px solid var(--color-border-sidebar, #e8edf2);
            ">
                ${stream_badge}
                <span style="font-size: 14px; font-weight: 600; flex:1; min-width:0;
                    color: var(--color-text-default);">${topic.topic}</span>
                ${mention_badge}
            </div>
            ${msgs_html}
            ${footer}
        </div>
    `);
}

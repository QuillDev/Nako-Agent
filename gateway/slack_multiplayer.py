"""Slack multiplayer routing and progress overrides used by the adapter and gateway."""

from __future__ import annotations

import inspect
import logging
import re
import time
from functools import wraps

logger = logging.getLogger(__name__)

_PLUGIN = "slack-multiplayer-enhancements"


def enabled() -> bool:
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly().get("plugins") or {}
    return _PLUGIN in (config.get("enabled") or ()) and _PLUGIN not in (config.get("disabled") or ())


def _opt_in(make):
    """Keep stock behavior for profiles that did not enable the standalone plugin."""
    def decorate(original):
        enhanced = make(original)
        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def scoped(*args, **kwargs):
                return await (enhanced if enabled() else original)(*args, **kwargs)
        else:
            @wraps(original)
            def scoped(*args, **kwargs):
                return (enhanced if enabled() else original)(*args, **kwargs)
        return scoped
    return decorate


@_opt_in
def humanize_mentions(original):
    async def enhanced(self, text, chat_id="", team_id=""):
        return await humanize_user_mentions(self, text, chat_id, team_id)
    return enhanced

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
# A thread copy read this recently is trusted without another conversations.replies call.
_FRESH_THREAD_SECONDS = 5.0


# ----- mention targets -------------------------------------------------------


def _self_uids(adapter, chat_id: str, team_id: str) -> set:
    team_id = str(team_id or getattr(adapter, "_channel_team", {}).get(chat_id, ""))
    uids = {getattr(adapter, "_bot_user_id", None)}
    uids.add((getattr(adapter, "_team_bot_user_ids", None) or {}).get(team_id))
    return {u for u in uids if u}


async def humanize_user_mentions(self, text: str, chat_id: str = "", team_id: str = "") -> str:
    """``<@UID>`` → ``@Name (UID)``; our own bot id → ``@Name (you)``."""
    if not text or "<@" not in text:
        return text
    mine = _self_uids(self, chat_id, team_id)
    labels: dict[str, str] = {}
    for uid in dict.fromkeys(_MENTION_RE.findall(text)):
        name = await self._resolve_user_name(uid, chat_id=chat_id, team_id=team_id)
        display = (name or "").strip() or uid
        if uid in mine:
            labels[uid] = f"@{display} (you)"
        elif display == uid:
            labels[uid] = f"@{uid}"
        else:
            labels[uid] = f"@{display} ({uid})"
    # Function replacement: user-set names are inserted verbatim, never parsed as templates.
    return _MENTION_RE.sub(lambda m: labels.get(m.group(1), m.group(0)), text)


@_opt_in
def _make_apply_bot_mention(original):
    @wraps(original)
    def _apply_bot_mention(self, text, original_text, command_probe_text, is_command_text,
                           bot_uid, thread_ts, team_id):
        result = original(self, text, original_text, command_probe_text, is_command_text,
                          bot_uid, thread_ts, team_id)
        new_text, new_original, new_probe, new_is_command = result
        if not new_text:
            # A bare ping stays empty as before. Commands are unaffected either way: the event
            # text for a command is the canonical command_probe_text.
            return result
        # Keep our mention in place; _humanize_user_mentions marks it "(you)".
        return text.strip(), new_original, new_probe, new_is_command
    return _apply_bot_mention


_OTHER_BOT_NAME = bytes((72, 101, 114, 109, 101, 115)).decode("ascii")
_IDENTITY_NOTE = (
    ' Mentions in message text are shown as "@Name (USERID)"; a mention of you is shown as '
    '"@Name (you)". When a message @mentions someone else and not you, it is addressed to '
    "them, even if their name looks generic (for example another bot named \"bot\")."
    f' Other bots here may be named "{_OTHER_BOT_NAME}"; that name never refers to you, so a message '
    f'addressed to {_OTHER_BOT_NAME} is not for you.')


# ----- thread history mentions ---------------------------------------------------


@_opt_in
def _make_thread_context_line(original):
    @wraps(original)
    async def _thread_context_line(self, msg, msg_text, is_parent, team_id, channel_id, **kwargs):
        # Core strips our own mention before this runs. Put it back when that is the only
        # difference, so it can read "(you)", then humanize like a live message.
        bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id) or ""
        if bot_uid and isinstance(msg, dict):
            try:
                full = self._render_message_text(msg)
            except Exception:
                full = ""
            if full and full.replace(f"<@{bot_uid}>", "").strip() == msg_text:
                msg_text = full
        msg_text = await self._humanize_user_mentions(msg_text, chat_id=channel_id, team_id=team_id)
        return await original(self, msg, msg_text, is_parent, team_id, channel_id, **kwargs)
    return _thread_context_line


# ----- messages addressed to someone else -------------------------------------


async def _addressed_elsewhere(adapter, event: dict) -> bool:
    """True for a channel message meant for someone else: it @mentions others but not us, or
    it's an untagged reply in a thread another bot started."""
    if not isinstance(event, dict) or event.get("channel_type") == "im":
        return False
    if event.get("subtype") not in (None, "", "thread_broadcast", "file_share"):
        return False
    chat_id = str(event.get("channel") or "")
    team_id = str(event.get("team") or event.get("team_id") or "")
    mine = _self_uids(adapter, chat_id, team_id)
    if not mine:
        return False
    mentioned = set(_MENTION_RE.findall(str(event.get("text") or "")))
    if mentioned & mine:
        return False
    if mentioned:
        return True
    ts, thread_ts = event.get("ts"), event.get("thread_ts")
    parent = event.get("parent_user_id")
    if not thread_ts or thread_ts == ts or not parent or parent in mine:
        return False
    if not await adapter._resolve_user_is_bot(parent, chat_id=chat_id, team_id=team_id):
        return False
    if _tagged_in_thread(adapter, thread_ts, team_id):
        return False
    return not await _bot_replied_in_thread(adapter, chat_id, thread_ts, team_id, refresh=True)


def _tagged_in_thread(adapter, thread_ts: str, team_id: str) -> bool:
    """True when someone tagged us in the thread during this run (core remembers it in memory).
    It covers a follow-up sent while our reply to that tag is still being written."""
    seen = getattr(adapter, "_mentioned_threads", None) or ()
    return adapter._workspace_message_marker(team_id, thread_ts) in seen or thread_ts in seen


def _mark_dropped_as_seen(adapter, event: dict, payload) -> None:
    """Record a dropped message's ts the way core's prefilter would have. Slack sends an edit
    that adds a mention twice (an app_mention under the original ts plus a message_changed);
    core lets only one through because the original ts is already seen. Dropping before core
    skipped that, so both copies ran and the second failed admission as a conflict."""
    ts = event.get("ts")
    if not ts:
        return
    try:
        adapter._dedup.is_duplicate(
            adapter._workspace_event_id(adapter._event_team_id(event, payload), ts))
    except Exception:
        logger.exception("slack-multiplayer-enhancements: could not mark a dropped message as seen")


@_opt_in
def _make_handle_slack_message(original):
    @wraps(original)
    async def _handle_slack_message(self, event, payload=None):
        if await _addressed_elsewhere(self, event):
            _mark_dropped_as_seen(self, event, payload)
            return None
        return await original(self, event, payload)
    return _handle_slack_message


# ----- wake on threads we posted in --------------------------------------------


async def _bot_replied_in_thread(adapter, channel_id: str, thread_ts: str, team_id: str = "",
                                 refresh: bool = False) -> bool:
    """True when any message in the thread was posted by this bot. Reads conversations.replies
    through the adapter's thread cache, fetching once on a miss. A cached copy can predate our
    own reply (it is saved when the thread is first read), so with ``refresh`` a cached "no"
    older than a few seconds is re-read from Slack before it counts."""
    if not thread_ts:
        return False
    bot_uid = adapter._team_bot_user_ids.get(team_id, adapter._bot_user_id) or ""
    if not bot_uid:
        return False

    def cached():
        for key, entry in adapter._thread_context_cache.items():
            if key.startswith(f"{channel_id}:{thread_ts}:"):
                return entry
        return None

    entry = cached()
    if entry is not None:
        if any((m or {}).get("user") == bot_uid for m in (entry.messages or [])):
            return True
        if not refresh or time.monotonic() - entry.fetched_at < _FRESH_THREAD_SECONDS:
            return False
    await adapter._fetch_thread_context(channel_id=channel_id, thread_ts=thread_ts, current_ts="",
                                        team_id=team_id, force_refresh=entry is not None)
    entry = cached()
    return entry is not None and any(
        (m or {}).get("user") == bot_uid for m in (entry.messages or []))


@_opt_in
def _make_should_wake(original):
    signature = inspect.signature(original)

    @wraps(original)
    async def _should_wake_on_unmentioned_message(self, *args, **kwargs):
        if await original(self, *args, **kwargs):
            return True
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        a = bound.arguments
        return bool(a["is_thread_reply"] and await _bot_replied_in_thread(
            self, a["channel_id"], a["event_thread_ts"], a.get("team_id") or "", refresh=True))
    return _should_wake_on_unmentioned_message


@_opt_in
def _make_build_identity_prompt(original):
    @wraps(original)
    def _build_identity_prompt(self, team_id: str = "") -> str:
        base = original(self, team_id)
        return base + _IDENTITY_NOTE if base else base
    return _build_identity_prompt

_PLAN_TITLE = "working on it"
# Slack allows chat.appendStream at Tier 4 (100+/min); one frame a second stays well under it.
_CARD_MIN_INTERVAL = 1.0
_CARD_TS_ATTR = "_sme_card_ts"
_CARD_LABELS = (
    (("send_message",), "sending a message"),
    (("slack",), "checking slack"),
    (("web_search", "search_web"), "searching the web"),
    (("web_extract", "browser", "browse", "fetch"), "reading a web page"),
    (("query", "sql", "snowflake", "autopat", "sigma"), "running a query"),
    (("terminal", "execute_code", "process", "shell"), "running a command"),
    (("write_file", "patch", "edit"), "editing files"),
    (("read_file", "search_files", "file"), "reading files"),
    (("session_search",), "looking through past chats"),
    (("skill", "memory"), "checking notes"),
    (("vision", "image"), "looking at an image"),
)


def _card_label(tool_name) -> str:
    """A short plain label for a tool's kind: never its name, arguments or results."""
    name = str(tool_name or "").lower()
    for needles, label in _CARD_LABELS:
        if any(n in name for n in needles):
            return label
    return "using a tool"


@_opt_in
def _make_apply_event(original):
    @wraps(original)
    def apply_event(self, raw):
        if isinstance(raw, dict) and raw.get("type") in {"tool.started", "tool.completed"}:
            raw = {"type": raw["type"], "tool_call_id": raw.get("tool_call_id"),
                   "tool_name": _card_label(raw.get("tool_name")), "is_error": raw.get("is_error")}
        return original(self, raw)
    return apply_event


@_opt_in
def _make_task_card_publish(original):
    import asyncio

    async def _task_card_publish(self, st):
        # Coalesce: wait out the interval, then fold every queued event into one frame (each
        # frame carries the whole task list). The turn-end flush runs while cancelling: no wait.
        last = getattr(st, "_sme_last_publish", None)
        task = asyncio.current_task()
        cancelling = bool(task and task.cancelling())
        cancelled = None
        if last is not None and not cancelling:
            wait = last + _CARD_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError as exc:
                    # The turn ended mid-wait. This frame's events are already out of the
                    # queue, so core's own last flush won't see them: send them now.
                    cancelled = exc
                self._task_card_drain(st)
                if cancelled is not None and not (
                        self._ctx._run_still_current() and not self._agent_interrupted()):
                    raise cancelled
        st._sme_last_publish = time.monotonic()
        # Core shows the last 8 tasks. A task that finished between frames and already scrolled
        # out would keep its spinner forever, so send its final state once too.
        published = st.__dict__.setdefault("_sme_published", {})
        window = type(st).visible_tasks(st)
        shown = {task["id"] for task in window}
        settled = [st.tasks[i] for i in st.task_order
                   if i not in shown and published.get(i) != st.tasks[i]["status"]]
        frame = settled + window
        st.visible_tasks = lambda: frame
        try:
            result = await original(self, st)
        finally:
            del st.visible_tasks
        if not getattr(st, "publication_suppressed", False):
            published.update({task["id"]: task["status"] for task in frame})
        if cancelled is not None:
            raise cancelled
        return result
    return _task_card_publish


@_opt_in
def _make_task_card_fallback(original):
    @wraps(original)
    async def _task_card_send_or_edit_fallback(self, st):
        # Core turns a failed card into a text progress bubble. Without an operator-written
        # tool_progress mode that is new chatter in the channel, so stay quiet instead.
        if not getattr(self._ctx, "tool_progress_enabled", False):
            return None
        return await original(self, st)
    return _task_card_send_or_edit_fallback


def _turn_was_silent(ctx) -> bool:
    try:
        from gateway.response_filters import is_intentional_silence_agent_result
        result = (getattr(ctx, "result_holder", None) or [None])[0]
        if not isinstance(result, dict):
            return False
        return bool(is_intentional_silence_agent_result(result, result.get("final_response")))
    except Exception:
        logger.exception("slack-multiplayer-enhancements: silent-turn check failed")
        return False


async def _drop_turn_cards(runner, adapter) -> None:
    """Forget this turn's cards; delete them when the turn ended silent (NO_REPLY)."""
    ctx = runner._ctx
    key_fn = getattr(adapter, "_native_task_card_key", None)
    cards = getattr(adapter, _CARD_TS_ATTR, None)
    if not callable(key_fn) or not isinstance(cards, dict):
        return
    key = key_fn(ctx.source.chat_id, ctx._progress_reply_to, ctx._progress_metadata)
    stamps = cards.pop(key, [])
    if not stamps or not _turn_was_silent(ctx):
        return
    client = adapter._get_client(key[1], team_id=key[0])
    for ts in stamps:
        try:
            await client.api_call("chat.delete", json={"channel": key[1], "ts": ts})
        except Exception as exc:
            logger.warning("slack-multiplayer-enhancements: couldn't delete a silent turn's card: %s", exc)


@_opt_in
def _make_send_native_task_card_progress_runner(original):
    @wraps(original)
    async def _send_native_task_card_progress(self, adapter):
        try:
            return await original(self, adapter)
        finally:
            try:
                await _drop_turn_cards(self, adapter)
            except Exception:
                logger.exception("slack-multiplayer-enhancements: card cleanup failed")
    return _send_native_task_card_progress


@_opt_in
def _make_send_native_task_card_progress(original):
    @wraps(original)
    async def send_native_task_card_progress(self, chat_id, tasks, *, title=None, reply_to=None,
                                             metadata=None, fallback_text=None):
        result = await original(self, chat_id, tasks, title=_PLAN_TITLE, reply_to=reply_to,
                                metadata=metadata, fallback_text=fallback_text)
        stamp = getattr(result, "message_id", None) if getattr(result, "success", False) else None
        if stamp:
            key = self._native_task_card_key(chat_id, reply_to, metadata)
            cards = self.__dict__.setdefault(_CARD_TS_ATTR, {})
            seen = cards.setdefault(key, [])
            if stamp not in seen:
                seen.append(stamp)
        return result
    return send_native_task_card_progress


@_opt_in
def _make_on_processing_start(original):
    @wraps(original)
    async def on_processing_start(self, event):
        # The "is working" status under the message shows the turn instead of the eyes, in DMs and
        # threads. Slack shows no status under a top-level channel message, so that keeps the eyes.
        source = getattr(event, "source", None)
        if not _shows_status(str(getattr(source, "chat_id", "") or ""), getattr(source, "thread_id", None),
                             getattr(event, "message_id", None)):
            return await original(self, event)
        return None
    return on_processing_start

def _shows_status(chat_id, thread_id, message_id) -> bool:
    """Slack draws the status in a thread, and under a top-level DM message; not under a
    top-level channel message."""
    in_thread = bool(thread_id) and str(thread_id) != str(message_id)
    return in_thread or str(chat_id).startswith("D")


def status_metadata_for_turn(self, chat_id, anchor, metadata):
    """Core's turn-status hook: with flat replies a top-level message names no thread, so core
    showed no status for it. Slack shows a status set on the message's own ts right under it,
    without opening a thread, so a message that would have got the eyes gets it there."""
    md = dict(metadata or {})
    if md.get("thread_id") or md.get("thread_ts") or self.config.extra.get("reply_in_thread", True) \
            or not _shows_status(chat_id, None, anchor):
        return md
    marker = self._workspace_message_marker(str(md.get("slack_team_id") or ""), str(anchor))
    if marker in self._reacting_message_ids:
        md["thread_id"] = str(anchor)
        anchors = self.__dict__.setdefault(_SESSION_STATUS_ATTR, {})
        anchors[(str(chat_id), str(anchor))] = True
        while len(anchors) > _SESSION_STATUS_MAX:
            anchors.pop(next(iter(anchors)))
    return md


# A text status (assistant.threads.setStatus) on a top-level DM message makes Slack desktop pop
# that message's thread panel open. The agent-session status shows a working line under it
# without the panel, but takes only a state, so top-level DM messages get that instead.
_SESSION_STATUS_ATTR = "_sme_session_status"
_SESSION_STATUS_MAX = 500


@_opt_in
def _make_set_thread_status(original):
    @wraps(original)
    async def _set_thread_status(self, chat_id, team_id, thread_ts, status, fail_label):
        if (str(chat_id), str(thread_ts)) not in self.__dict__.get(_SESSION_STATUS_ATTR, {}):
            return await original(self, chat_id, team_id, thread_ts, status, fail_label)
        try:
            await self._get_client(chat_id, team_id=team_id).api_call("agents.sessions.setStatus", json={
                "channel_id": chat_id, "thread_ts": thread_ts, "status": "processing" if status else "active"})
        except Exception as exc:
            logger.debug("slack-multiplayer-enhancements: agent session status %s: %s", fail_label, exc)
    return _set_thread_status


# A message that doesn't ask the bot (an untagged reply in a busy thread) still runs a turn, and
# core showed "is working" for it at once, which reads like a reply on the way. Such a turn holds
# the status back until its first tool starts; one that answers NO_REPLY never shows it.
_HELD_STATUS_ATTR = "_sme_held_status"
_HELD_STATUS_MAX = 500


def _status_key(chat_id, metadata):
    md = metadata or {}
    return str(chat_id), str(md.get("thread_id") or md.get("thread_ts") or "")


def _holds_status(event) -> bool:
    source = getattr(event, "source", None)
    return getattr(event, "reply_expected", None) is not True and getattr(source, "chat_type", "") != "dm"


@_opt_in
def _make_start_typing_refresh(original):
    @wraps(original)
    def _start_typing_refresh(self, event, interrupt_event, metadata):
        task = original(self, event, interrupt_event, metadata)
        # A key stays held until its status shows, so the clears after a silent turn send nothing.
        held = self.__dict__.setdefault(_HELD_STATUS_ATTR, {})
        key = _status_key(event.source.chat_id, metadata)
        held.pop(key, None)
        if task is not None and _holds_status(event):
            held[key] = True
            while len(held) > _HELD_STATUS_MAX:
                held.pop(next(iter(held)))
        return task
    return _start_typing_refresh


@_opt_in
def _make_send_typing(original):
    @wraps(original)
    async def send_typing(self, chat_id, metadata=None):
        if _status_key(chat_id, metadata) in self.__dict__.get(_HELD_STATUS_ATTR, {}):
            return  # no tool has started yet
        return await original(self, chat_id, metadata)
    return send_typing


@_opt_in
def _make_set_status_text(original):
    @wraps(original)
    def set_status_text(self, chat_id, text):
        # A tool started: release this chat's held statuses (status text is per chat in core too).
        if text:
            held = self.__dict__.get(_HELD_STATUS_ATTR, {})
            for key in [k for k in held if k[0] == str(chat_id)]:
                held.pop(key, None)
        return original(self, chat_id, text)
    return set_status_text


@_opt_in
def _make_stop_typing(original):
    @wraps(original)
    async def stop_typing(self, chat_id, metadata=None):
        if _status_key(chat_id, metadata) in self.__dict__.get(_HELD_STATUS_ATTR, {}):
            return  # never shown, nothing to clear
        return await original(self, chat_id, metadata)
    return stop_typing

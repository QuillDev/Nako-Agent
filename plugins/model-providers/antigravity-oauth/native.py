"""Native tool calling for ``agy acp``: the host's tools reach the model as real functions.

agy keeps the conversation and calls the host's tools through a local MCP server
(:class:`ToolBridge`). A model function call arrives there, goes back to the host as an ordinary
tool call, and is answered when the host sends that tool's result on its next request. One agy
turn therefore spans several host requests, and nothing is replayed as text while the
conversation only grows. A conversation that changed some other way is rebuilt once, in a new
agy session.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

try:
    from .acp import (
        _OUTPUT_LIMIT_STOP_REASONS,
        AntigravityACPStream,
        _format_rpc_error,
        _usage_from_acp,
        spawn_acp_worker,
    )
    from .alias import mask as _mask, unmask as _unmask
    from .host import TITLE as _HOST_TITLE
    from .process import build_child_env, setup_isolated_home
    from .prompt import (
        _MAX_PROMPT_BYTES,
        _MAX_PROMPT_CHARS,
        _extract_images_from_content,
        _extract_images_from_messages,
        _fit_transcript_to_budget,
        _messages_match_prefix,
        _render_message_content,
    )
except ImportError:  # flat source tree under test
    from acp import (
        _OUTPUT_LIMIT_STOP_REASONS,
        AntigravityACPStream,
        _format_rpc_error,
        _usage_from_acp,
        spawn_acp_worker,
    )
    from alias import mask as _mask, unmask as _unmask
    from host import TITLE as _HOST_TITLE
    from process import build_child_env, setup_isolated_home
    from prompt import (
        _MAX_PROMPT_BYTES,
        _MAX_PROMPT_CHARS,
        _extract_images_from_content,
        _extract_images_from_messages,
        _fit_transcript_to_budget,
        _messages_match_prefix,
        _render_message_content,
    )

logger = logging.getLogger(__name__)

SERVER = "host"
_TOOL_PREFIX = f"mcp_{SERVER}_"
_SHIM = str(Path(__file__).resolve().with_name("mcp_shim.py"))
# agy retries a rate-limited model call on its own for a little over two minutes without a word.
_DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0
_MAX_BUILTIN_TOOL_REMINDERS = 2
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_CONTRACT = (
    f"This session is driven by {_HOST_TITLE} Agent, a host program. Its instructions say who you are "
    "and how to work; follow them in place of your default persona. "
    f"Your tools are the functions named {_TOOL_PREFIX}*. The host runs them and returns the real results. "
    "Built-in tools (run_command, file reads and edits, web access, subagents, tasks) are switched off "
    "here: calling one ends your turn without doing anything. The one exception is read_file on an image "
    f"({', '.join(suffix[1:] for suffix in _IMAGE_SUFFIXES)}): to look at an image on disk, open it with "
    "read_file yourself rather than asking a host tool to load or describe it. When a tool result says an "
    "image was offloaded to a file, it is not in your context yet: open that file the same way."
)


def enabled() -> bool:
    return os.getenv("ANTIGRAVITY_NATIVE_TOOLS", "1").strip().lower() not in {"0", "false", "no", "off"}


def idle_timeout() -> float:
    try:
        value = float(os.getenv("ANTIGRAVITY_IDLE_TIMEOUT_SECONDS", ""))
    except ValueError:
        return _DEFAULT_IDLE_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_IDLE_TIMEOUT_SECONDS


def _session_meta(initialized: dict[str, Any]) -> dict[str, Any] | None:
    """Ask agy to append the tool contract to its system prompt, where the runtime offers that."""
    extension = ((initialized.get("agentCapabilities") or {}).get("_meta") or {}).get("antigravity")
    system_prompt = extension.get("systemPrompt") if isinstance(extension, dict) else None
    if isinstance(system_prompt, dict) and system_prompt.get("append") is True:
        return {"antigravity": {"systemPrompt": {"append": _mask(_CONTRACT)}}}
    return None


def _mcp_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI function tools as MCP tool declarations."""
    declared = []
    for tool in tools or ():
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        declared.append({
            "name": fn["name"],
            "description": fn.get("description") or "",
            "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return json.loads(_mask(json.dumps(declared, ensure_ascii=False)))


class PendingCall:
    """One model function call waiting for the host's result."""

    def __init__(self, connection: "_Connection", rpc_id: Any, name: str, arguments: dict[str, Any]) -> None:
        self.id = f"call_{uuid.uuid4().hex[:16]}"
        self.connection = connection
        self.rpc_id = rpc_id
        self.name = name
        self.arguments = arguments


class _Connection:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._write_lock = threading.Lock()

    def send(self, obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            self.sock.sendall(data)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


class ToolBridge:
    """Local MCP server for the host's tools. A call is passed to ``sink`` and answered by :meth:`resolve`."""

    def __init__(self) -> None:
        self.token = secrets.token_hex(16)
        self.tools: list[dict[str, Any]] = []
        self.sink: Any = None
        self._closed = False
        self._connections: set[_Connection] = set()
        self._lock = threading.Lock()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.address = "127.0.0.1:%d" % self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True, name="agy-tool-bridge").start()

    def server_config(self) -> dict[str, Any]:
        """This server's entry for agy's ``mcp_config.json``.

        The entry has to come from the file: a server passed in ``session/new`` keeps agy's three
        minute limit per call, and the host's tools (a long command, an approval) can take longer.
        """
        return {
            "command": sys.executable,
            "args": [_SHIM],
            "env": {"ANTIGRAVITY_BRIDGE_ADDR": self.address, "ANTIGRAVITY_BRIDGE_TOKEN": self.token},
            "timeoutSeconds": -1,
            # Lazy tools are only named in the system prompt; eager ones are declared as functions.
            "tools": {tool["name"]: {"eager": True} for tool in self.tools},
        }

    def _accept(self) -> None:
        while not self._closed:
            try:
                sock, _ = self._listener.accept()
            except OSError:
                return
            connection = _Connection(sock)
            with self._lock:
                self._connections.add(connection)
            threading.Thread(target=self._serve, args=(connection,), daemon=True, name="agy-tool-bridge-conn").start()

    def _serve(self, connection: _Connection) -> None:
        try:
            with connection.sock.makefile("r", encoding="utf-8", errors="replace") as lines:
                if not secrets.compare_digest(lines.readline().strip(), self.token):
                    return
                for line in lines:
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(message, dict):
                        self._handle(connection, message)
        except OSError:
            pass
        finally:
            with self._lock:
                self._connections.discard(connection)
            connection.close()

    def _handle(self, connection: _Connection, message: dict[str, Any]) -> None:
        rpc_id = message.get("id")
        if rpc_id is None or "method" not in message:
            return  # a notification, or a reply to something this server never asks
        method = message["method"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "tools/call":
            arguments = params.get("arguments")
            call = PendingCall(connection, rpc_id, str(params.get("name") or ""),
                               arguments if isinstance(arguments, dict) else {})
            sink = self.sink
            if sink is None:
                self.resolve(call, [{"type": "text", "text": "No host turn is waiting for this call."}], is_error=True)
            else:
                sink({"_host_tool_call": call})
            return
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": params.get("protocolVersion") or "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER, "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "ping":
            result = {}
        else:
            connection.send({"jsonrpc": "2.0", "id": rpc_id,
                             "error": {"code": -32601, "message": f"Method not supported: {method}"}})
            return
        connection.send({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def resolve(self, call: PendingCall, content: list[dict[str, Any]], *, is_error: bool = False) -> None:
        """Answer ``call``. Raises ``OSError`` when agy has already dropped the connection."""
        call.connection.send({"jsonrpc": "2.0", "id": call.rpc_id,
                              "result": {"content": content, "isError": is_error}})

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(OSError):
            self._listener.close()
        with self._lock:
            connections = tuple(self._connections)
        for connection in connections:
            connection.close()


def _role(message: dict[str, Any]) -> str:
    return str(message.get("role") or "").strip().lower()


def _same_request(history: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> bool:
    return bool(history) and len(history) == len(incoming) and all(
        a.get(key) == b.get(key) for a, b in zip(history, incoming) for key in ("role", "content", "tool_calls")
    )


def _shortened(text: str, head: int, tail: int, what: str) -> str:
    if len(text) <= head + tail + 40:
        return text
    return text[:head] + f"\n[... {what} shortened: {len(text)} chars ...]\n" + text[-tail:]


def _transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Earlier turns as plain text entries; ``compact`` is what an entry shrinks to when space runs out."""
    names: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    for message in messages:
        role = _role(message)
        text = _render_message_content(message.get("content"))
        if role == "tool":
            label = names.get(str(message.get("tool_call_id") or ""), "") or str(message.get("name") or "tool")
            items.append({"role": role, "full": f"Tool result ({label}):\n{text}",
                          "compact": f"Tool result ({label}):\n{_shortened(text, 200, 100, 'tool output')}"})
            continue
        if role == "assistant":
            full, compact = ([text], [_shortened(text, 700, 300, "reply")]) if text else ([], [])
            for tool_call in message.get("tool_calls") or ():
                fn = tool_call.get("function") if isinstance(tool_call, dict) else None
                if not isinstance(fn, dict):
                    continue
                names[str(tool_call.get("id") or "")] = str(fn.get("name") or "")
                arguments = fn.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                full.append(f"[called {fn.get('name')} with {arguments}]")
                compact.append(f"[called {fn.get('name')} with {_shortened(arguments, 300, 150, 'arguments')}]")
            if full:
                items.append({"role": role, "full": "Assistant:\n" + "\n".join(full),
                              "compact": "Assistant:\n" + "\n".join(compact)})
            continue
        if text:
            entry = f"{'User' if role == 'user' else 'Context'}:\n{text}"
            items.append({"role": role, "full": entry, "compact": entry})
    return items


def opening_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first prompt of an agy session: the host's instructions, any earlier turns, the request."""
    system = [text for m in messages if _role(m) == "system" and (text := _render_message_content(m.get("content")))]
    conversation = [m for m in messages if _role(m) != "system"]
    last_role = _role(conversation[-1]) if conversation else ""
    head = [_CONTRACT]
    if system:
        head.append("### HOST INSTRUCTIONS\n" + "\n\n".join(system))
    if last_role == "user":
        earlier = conversation[:-1]
        footer = "### USER REQUEST\n" + _render_message_content(conversation[-1].get("content"))
    else:
        earlier = conversation
        footer = "Carry on with the task from the latest message above."
    kept = _fit_transcript_to_budget(_transcript(earlier), "\n\n".join([*head, footer]),
                                     _MAX_PROMPT_CHARS, _MAX_PROMPT_BYTES)
    sections = [*head, *(["### CONVERSATION SO FAR\n\n" + "\n\n".join(kept)] if kept else []), footer]
    return [{"type": "text", "text": "\n\n".join(sections)}, *_extract_images_from_messages(messages)]


def _labelled(messages: list[dict[str, Any]]) -> str:
    parts = []
    for message in messages:
        text = _render_message_content(message.get("content"))
        if text and _role(message) != "assistant":
            parts.append(text if _role(message) == "user" else f"Context:\n{text}")
    return "\n\n".join(parts)


def _follow_up_blocks(delta: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the user said after the model's last reply, for a session that already holds the rest."""
    text = _labelled(delta)
    images = _extract_images_from_messages(delta)
    if not text and not images:
        return []
    return [{"type": "text", "text": text or "User attached an image."}, *images]


def _result_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    text = _render_message_content(message.get("content"))
    return [{"type": "text", "text": _mask(text)}, *_extract_images_from_content(message.get("content"))]


def _parsed_result(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _merged_result_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(messages) == 1:
        return _result_content(messages[0])
    entries: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        entry: dict[str, Any] = {"index": index}
        name = str(message.get("name") or "")
        if name:
            entry["name"] = name
        entry["result"] = _parsed_result(_render_message_content(content))
        entries.append(entry)
        images.extend(_extract_images_from_content(content))
    return [{"type": "text", "text": _mask(json.dumps({"results": entries}, ensure_ascii=False))}, *images]


class NativeSession:
    """The agy session behind one client, and what the host has last seen of it."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.bridge: ToolBridge | None = None
        self.worker: Any = None
        self.model: str | None = None
        self.effort: str | None = None
        self.tools_key = ""
        self.history: list[dict[str, Any]] = []
        self.prompt_id: int | None = None  # the agy turn still running, if any
        self.steered_from: int | None = None  # a prompt a steer replaced; it still has to end
        self.pending: list[PendingCall] = []  # calls handed to the host and not answered yet
        self.last_reply: dict[str, Any] | None = None
        self.context_tokens = 0

    def _alive(self, model: str, effort: str | None, tools_key: str) -> bool:
        return (self.worker is not None and self.worker.proc.poll() is None
                and (self.model, self.effort, self.tools_key) == (model, effort, tools_key))

    def _spawn(self, model: str, effort: str | None, tools: list[dict[str, Any]], tools_key: str) -> None:
        client = self.client
        if self.bridge is None:
            self.bridge = ToolBridge()
        self.bridge.tools = tools
        workspace = os.path.join(client._cwd, "native")
        home, _ = setup_isolated_home(workspace)
        config_dir = Path(home) / ".gemini" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {SERVER: self.bridge.server_config()}}), encoding="utf-8")
        worker = spawn_acp_worker(
            command=client._command, args=client._args, cwd=workspace, env=build_child_env(home),
            model=model, effort=effort, terminate_fn=client._terminate_process,
            popen_fn=subprocess.Popen, session_meta=_session_meta,
        )
        with client._lock:
            client._active_processes.add(worker.proc)
        self.bridge.sink = worker.incoming.put
        self.worker, self.model, self.effort, self.tools_key = worker, model, effort, tools_key

    def reset(self) -> None:
        """Drop the agy session. The next request starts a new one from the host's own history."""
        worker, self.worker = self.worker, None
        pending, self.pending = self.pending, []
        self.history, self.prompt_id, self.last_reply, self.context_tokens = [], None, None, 0
        self.steered_from = None
        if self.bridge is not None:
            self.bridge.sink = None
        if worker is None:
            return
        worker.cancel_turn()
        for call in pending:
            with contextlib.suppress(OSError):
                self.bridge.resolve(call, [{"type": "text", "text": "The host cancelled this call."}], is_error=True)
        with self.client._lock:
            self.client._active_processes.discard(worker.proc)
        worker.close()

    def close(self) -> None:
        self.reset()
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None

    def _answers(self, delta: list[dict[str, Any]]) -> list[tuple[PendingCall, list[dict[str, Any]]]] | None:
        """Each pending call with its result(s) from ``delta``; None when the host skipped one."""
        tool_messages = [m for m in delta if _role(m) == "tool"]
        results = {str(m.get("tool_call_id") or ""): m for m in tool_messages}
        if any(call.id not in results for call in self.pending):
            return None
        pending_ids = {call.id for call in self.pending}
        emitted_ids = [
            str(tc.get("id") or "")
            for m in delta if _role(m) == "assistant"
            for tc in (m.get("tool_calls") or ())
            if isinstance(tc, dict) and tc.get("id")
        ]
        # The host splits a local tool_call batch into one assistant tool_call per entry, keeping
        # the original call id on entry 0 and giving the rest new ids. Group those back onto their
        # owning pending call in emitted order so none of the expanded results are dropped.
        ordered_ids = (emitted_ids if emitted_ids and pending_ids.issubset(emitted_ids)
                       else [str(m.get("tool_call_id") or "") for m in tool_messages if m.get("tool_call_id")])
        grouped: dict[str, list[dict[str, Any]]] = {}
        current: str | None = None
        for call_id in ordered_ids:
            if call_id in pending_ids:
                current = call_id
                grouped[current] = [results[call_id]]
            elif current is not None:
                if call_id not in results:
                    return None
                grouped[current].append(results[call_id])
            else:
                return None
        return [(call, _merged_result_content(grouped[call.id])) for call in self.pending]

    def begin(self, messages: list[dict[str, Any]], model: str, effort: str | None,
              tools: list[dict[str, Any]] | None) -> bool:
        """Move agy on to this request. True when the last reply is to be given again as it was."""
        declared = _mcp_tools(tools)
        tools_key = hashlib.sha256(json.dumps(declared, sort_keys=True).encode("utf-8")).hexdigest()
        if self._alive(model, effort, tools_key):
            if self.last_reply is not None and _same_request(self.history, messages):
                return True  # the host is asking again for a reply it could not use
            if _messages_match_prefix(self.history, messages):
                delta = messages[len(self.history):]
                if self.pending:
                    answers = self._answers(delta)
                    if answers is not None:
                        try:
                            # What the user said while the tool ran. agy takes a prompt sent mid-turn
                            # as a steer: the running prompt ends empty and this one carries on.
                            if steer := _follow_up_blocks([m for m in delta if _role(m) != "tool"]):
                                self.steered_from = self.prompt_id
                                self.prompt_id = self.worker.send_prompt(steer)
                            for call, content in answers:
                                self.bridge.resolve(call, content)
                        except OSError:
                            answers = None
                    if answers is not None:
                        self.pending = []
                        return False
                elif self.prompt_id is None and (blocks := _follow_up_blocks(delta)):
                    self.prompt_id = self.worker.send_prompt(blocks)
                    return False
        self.reset()
        self._spawn(model, effort, declared, tools_key)
        self.prompt_id = self.worker.send_prompt(opening_blocks(messages))
        return False


def _is_host_tool(tool_call: dict[str, Any]) -> bool:
    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, dict) and isinstance(raw_input.get("CommandLine"), str):
        return False
    name = str(tool_call.get("name") or "")
    return name.startswith(_TOOL_PREFIX) or str(tool_call.get("title") or "").startswith(f"Run {_TOOL_PREFIX}")


def _reads_an_image(tool_call: dict[str, Any]) -> bool:
    raw_input = tool_call.get("rawInput")
    path = raw_input.get("Path") if isinstance(raw_input, dict) else None
    return (tool_call.get("name") == "read_file" and isinstance(path, str)
            and os.path.realpath(path).lower().endswith(_IMAGE_SUFFIXES))


class NativeToolStream(AntigravityACPStream):
    """One host request's share of an agy turn: up to the next tool call, or to the end of the turn."""

    def __init__(self, *, session: NativeSession, replay: bool, **kwargs: Any) -> None:
        self.session = session
        self._replay = replay
        super().__init__(worker=session.worker, req_id=0, is_worker=True, **kwargs)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._finished:
            self._interrupted = True
            self.session.reset()
        self._release_worker_lock_once()
        self._retire_executor()

    def _usage(self, raw: dict[str, Any]) -> SimpleNamespace:
        """Usage for this request. agy totals a turn's model calls, so the context size comes from its meter."""
        cached = int(raw.get("cachedReadTokens") or 0)
        prompt = self.session.context_tokens or int(raw.get("inputTokens") or 0) + cached
        cached = min(cached, prompt)
        return _usage_from_acp({"inputTokens": prompt - cached, "cachedReadTokens": cached,
                                "outputTokens": raw.get("outputTokens") or 0,
                                "thoughtTokens": raw.get("thoughtTokens")})

    def _close_out(self, reply: dict[str, Any]) -> Iterator[Any]:
        """Record the reply, then hand over its last chunks. Recorded first: a caller may stop reading early."""
        session = self.session
        session.history = list(self.messages or [])
        session.last_reply = reply
        self._finished = True
        self._release_worker_lock_once()
        yield from self._reply_chunks(reply, tail_only=True)

    def _reply_chunks(self, reply: dict[str, Any], *, tail_only: bool = False) -> Iterator[Any]:
        if not tail_only:
            if reply["content"]:
                yield self._make_chunk(content=reply["content"])
            for call in reply["calls"]:
                yield self._make_chunk(tool_calls=[_tool_call_delta(call)])
        yield self._make_chunk(finish_reason=reply["finish_reason"])
        yield SimpleNamespace(id=self.conversation_id or f"agy-{int(time.time() * 1000)}", choices=[],
                              model=self.model, usage=reply["usage"])

    def _stream_generator(self) -> Iterator[Any]:
        session, worker = self.session, self.worker
        try:
            if self._replay:
                self._finished = True
                self._release_worker_lock_once()
                yield from self._reply_chunks(session.last_reply)
                return
            deadline = time.monotonic() + self.timeout
            quiet_limit = idle_timeout()
            last_event = time.monotonic()
            text: list[str] = []
            refused = ""
            reminders = 0
            while True:
                if self._closed:
                    return
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError(f"Antigravity ACP stream timed out after {int(self.timeout)}s")
                if now - last_event >= quiet_limit:
                    raise TimeoutError(f"Antigravity sent nothing for {int(quiet_limit)}s")
                try:
                    message = worker.incoming.get(timeout=0.25)
                except queue.Empty:
                    continue
                if message is None:
                    stderr = worker.get_stderr()
                    raise RuntimeError("Antigravity execution failed: process exited"
                                       + (f"; stderr: {stderr}" if stderr else ""))
                last_event = time.monotonic()

                call = message.get("_host_tool_call")
                if call is not None:
                    session.pending.append(call)
                    yield self._make_chunk(tool_calls=[_tool_call_delta(call)])
                    yield from self._close_out({
                        "content": _unmask("".join(text)), "calls": [_frozen(call)], "finish_reason": "tool_calls",
                        "usage": self._usage({}),
                    })
                    return

                if "method" in message and "id" in message:
                    if message["method"] != "session/request_permission":
                        worker.respond_method_not_found(message["id"], str(message["method"]))
                        continue
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
                    allow = next((o.get("optionId") for o in params.get("options") or ()
                                  if isinstance(o, dict) and str(o.get("kind") or "").startswith("allow")), None)
                    if allow and (_is_host_tool(tool_call) or _reads_an_image(tool_call)):
                        worker.respond_permission(message["id"], allow)
                    else:
                        # Only the host runs tools; agy's own would act on this machine unasked.
                        worker.respond_permission_cancelled(message["id"])
                        refused = str(tool_call.get("name") or tool_call.get("title") or "a built-in tool")
                        logger.warning("Antigravity tried its built-in tool '%s'; refused.", refused)
                    continue

                if session.steered_from is not None and message.get("id") == session.steered_from:
                    session.steered_from = None
                    continue
                if message.get("id") == session.prompt_id and ("result" in message or "error" in message):
                    if "error" in message:
                        raise RuntimeError(f"Antigravity model error: {_format_rpc_error(message['error'])}")
                    session.prompt_id = None
                    result = message.get("result") if isinstance(message.get("result"), dict) else {}
                    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                    if str(result.get("stopReason") or "").strip().lower() in _OUTPUT_LIMIT_STOP_REASONS:
                        yield from self._close_out({"content": _unmask("".join(text)), "calls": [],
                                                    "finish_reason": "length", "usage": self._usage(usage)})
                        return
                    if not text and refused and reminders < _MAX_BUILTIN_TOOL_REMINDERS:
                        # A refused built-in ends agy's turn with nothing said; put the model back on track.
                        reminders += 1
                        session.prompt_id = worker.send_prompt([{"type": "text", "text": (
                            f"The built-in tool `{refused}` is switched off in this session, so that call did "
                            f"nothing. Do the same work with the {_TOOL_PREFIX}* functions and carry on.")}])
                        refused = ""
                        continue
                    if not text:
                        raise RuntimeError("Antigravity execution failed: empty result (status='SUCCESS')")
                    yield from self._close_out({"content": _unmask("".join(text)), "calls": [],
                                                "finish_reason": "stop", "usage": self._usage(usage)})
                    return

                if message.get("method") != "session/update":
                    continue
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                update = params.get("update") if isinstance(params.get("update"), dict) else {}
                kind = update.get("sessionUpdate")
                if kind == "usage_update":
                    session.context_tokens = int(update.get("used") or 0) or session.context_tokens
                    continue
                if kind not in ("agent_thought_chunk", "agent_message_chunk"):
                    continue
                content = update.get("content")
                delta = content.get("text") if isinstance(content, dict) else ""
                if not isinstance(delta, str) or not delta:
                    continue
                if kind == "agent_thought_chunk":
                    yield self._make_chunk(reasoning_content=delta)
                elif session.steered_from is None:  # the replaced prompt's last words would only repeat
                    text.append(delta)
                    yield self._make_chunk(content=delta)
        finally:
            if not self._finished:
                self.close()


def _frozen(call: PendingCall) -> dict[str, str]:
    return {"id": call.id, "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}


def _tool_call_delta(call: Any) -> SimpleNamespace:
    frozen = call if isinstance(call, dict) else _frozen(call)
    return SimpleNamespace(index=0, id=frozen["id"], type="function",
                           function=SimpleNamespace(name=frozen["name"], arguments=frozen["arguments"]))

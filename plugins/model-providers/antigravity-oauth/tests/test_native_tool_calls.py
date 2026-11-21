"""Behavior of native tool calling: a host request loop against a stand-in for ``agy acp``.

The stand-in follows what the real runtime was seen to do: it reads the MCP server from the
``mcp_config.json`` in its HOME, launches it, asks permission before each tool, only calls tools
that file marks eager, gives a call three minutes unless the file lifts that, and ends its turn
with nothing said when a built-in tool is refused.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

from client import AntigravityClient
from test_acp_vision import _FakeACPProcess

_REAL_POPEN = subprocess.Popen
TOOLS = [{"type": "function", "function": {
    "name": "lookup", "description": "Weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
}}]
SYSTEM = {"role": "system", "content": "You are the host's assistant."}


class _Agy(_FakeACPProcess):
    """One ``agy acp`` process. ``brain(agy, prompt_text)`` plays the model for each prompt."""

    def __init__(self, env: dict[str, str], brain) -> None:
        super().__init__(turn_handler=self._turn)
        self._env, self._brain = env, brain
        self._server: dict[str, Any] | None = None
        self._mcp: subprocess.Popen | None = None
        self._rpc = 0
        self.tool_calls_received = 0

    def _turn(self, proc, req_id: int, params: dict[str, Any]) -> None:
        self._brain(self, "".join(b.get("text", "") for b in params["prompt"]))
        self.emit({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn", "usage": {}}})

    def say(self, text: str) -> None:
        self.emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": self.session_id, "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})

    def _permitted(self, name: str, raw_input: dict[str, Any]) -> bool:
        self._rpc += 1
        request_id = 9000 + self._rpc
        self.emit({"jsonrpc": "2.0", "id": request_id, "method": "session/request_permission", "params": {
            "sessionId": self.session_id, "toolCall": {"name": name, "title": f"Run {name}", "rawInput": raw_input},
            "options": [{"optionId": "allow", "kind": "allow_once"}, {"optionId": "deny", "kind": "reject_once"}]}})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reply = next((r for r in self.server_replies if r.get("id") == request_id), None)
            if reply is not None:
                return (reply.get("result") or {}).get("outcome", {}).get("optionId") == "allow"
            time.sleep(0.01)
        raise AssertionError("the client never answered a permission request")

    def builtin(self, name: str, raw_input: dict[str, Any] | None = None) -> bool:
        return self._permitted(name, raw_input or {"CommandLine": "echo native"})

    def _mcp_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._rpc += 1
        self._mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._rpc, "method": method, "params": params}) + "\n")
        self._mcp.stdin.flush()
        return json.loads(self._mcp.stdout.readline())["result"]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if self._mcp is None:
            config = json.loads((Path(self._env["HOME"]) / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8-sig"))
            self._server = config["mcpServers"]["host"]
            self._mcp = _REAL_POPEN([self._server["command"], *self._server["args"]], text=True,
                                    env={**os.environ, **self._server["env"]}, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self._mcp_request("initialize", {"protocolVersion": "2025-03-26"})
        listed = {tool["name"] for tool in self._mcp_request("tools/list", {})["tools"]}
        if name not in listed or not self._server.get("tools", {}).get(name, {}).get("eager"):
            raise AssertionError(f"the model has no function for {name}")
        if self._server.get("timeoutSeconds") != -1:
            raise AssertionError("agy would give this call up after three minutes")
        if not self._permitted(f"mcp_host_{name}", arguments):
            raise AssertionError(f"the client refused its own tool {name}")
        self.tool_calls_received += 1
        result = self._mcp_request("tools/call", {"name": name, "arguments": arguments})
        return "".join(part.get("text", "") for part in result["content"])

    def terminate(self):
        if self._mcp is not None:
            self._mcp.kill()
        super().terminate()

    kill = terminate


class NativeToolCallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="agy_native_test_")
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"ANTIGRAVITY_IDLE_TIMEOUT_SECONDS": "5"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANTIGRAVITY_TRANSPORT", None)
        os.environ.pop("ANTIGRAVITY_NATIVE_TOOLS", None)
        self.spawned: list[_Agy] = []
        self.brain = None

        def spawn(cmd, **kw):
            self.spawned.append(_Agy(kw["env"], lambda agy, text: self.brain(agy, text)))
            return self.spawned[-1]

        for started in (patch("client.is_authenticated", return_value=True), patch("subprocess.Popen", side_effect=spawn)):
            started.start()
            self.addCleanup(started.stop)
        self.client = AntigravityClient(cwd=tmp.name, command="agy")
        self.addCleanup(self.client.close)

    def ask(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None):
        """One host request; the reply is appended to ``messages`` the way the host keeps it."""
        reply = self.client.chat.completions.create(
            model="gemini-4-neon-high", messages=list(messages), tools=TOOLS if tools is None else tools,
        ).choices[0]
        kept: dict[str, Any] = {"role": "assistant", "content": reply.message.content or ""}
        if reply.message.tool_calls:
            kept["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                  for c in reply.message.tool_calls]
        messages.append(kept)
        return reply

    @staticmethod
    def weather_brain(agy: _Agy, text: str) -> None:
        if "Tokyo" in text:
            agy.say("It is " + agy.call_tool("lookup", {"city": "Tokyo"}))
        else:
            agy.say("heard: " + text)

    def test_tool_call_goes_to_the_host_and_its_result_back_into_the_same_agy_turn(self):
        self.brain = self.weather_brain
        messages = [SYSTEM, {"role": "user", "content": "Weather in Tokyo?"}]

        first = self.ask(messages)
        self.assertEqual(first.finish_reason, "tool_calls")
        call = first.message.tool_calls[0]
        self.assertEqual((call.function.name, json.loads(call.function.arguments)), ("lookup", {"city": "Tokyo"}))

        messages.append({"role": "tool", "tool_call_id": call.id, "content": "sunny, 21C"})
        second = self.ask(messages)
        self.assertEqual((second.finish_reason, second.message.content), ("stop", "It is sunny, 21C"))

        messages.append({"role": "user", "content": "And tomorrow?"})
        third = self.ask(messages)
        # agy already holds the conversation, so it is sent the new message and nothing else.
        self.assertEqual(third.message.content, "heard: And tomorrow?")
        self.assertEqual(len(self.spawned), 1)

    def test_a_reply_the_host_asks_for_again_is_given_again_without_a_second_call(self):
        self.brain = self.weather_brain
        messages = [SYSTEM, {"role": "user", "content": "Weather in Tokyo?"}]
        first = self.ask(list(messages))
        again = self.ask(messages)
        self.assertEqual(again.message.tool_calls[0].id, first.message.tool_calls[0].id)

        messages.append({"role": "tool", "tool_call_id": again.message.tool_calls[0].id, "content": "sunny, 21C"})
        self.assertEqual(self.ask(messages).message.content, "It is sunny, 21C")
        self.assertEqual((len(self.spawned), self.spawned[0].tool_calls_received), (1, 1))

    def test_agy_built_in_tools_are_refused_and_the_model_carries_on_with_host_tools(self):
        ran_natively = []

        def brain(agy: _Agy, text: str) -> None:
            if "Weather" in text:
                if agy.builtin("run_command"):
                    ran_natively.append(True)
                return  # a refused built-in ends agy's turn with nothing said
            agy.say("It is " + agy.call_tool("lookup", {"city": "Tokyo"}))

        self.brain = brain
        messages = [SYSTEM, {"role": "user", "content": "Weather in Tokyo?"}]
        first = self.ask(messages)
        self.assertEqual(first.message.tool_calls[0].function.name, "lookup")
        messages.append({"role": "tool", "tool_call_id": first.message.tool_calls[0].id, "content": "sunny, 21C"})
        self.assertEqual(self.ask(messages).message.content, "It is sunny, 21C")
        self.assertEqual(ran_natively, [])

    def test_the_model_may_open_an_image_with_agy_s_reader_and_no_other_file(self):
        paths = ["/srv/shots/front.PNG", "/srv/shots/notes.txt", "/srv/shots/key.pem"]
        self.brain = lambda agy, text: agy.say(
            "opened: " + ", ".join(p for p in paths if agy.builtin("read_file", {"Path": p})))
        messages = [SYSTEM, {"role": "user", "content": "What is in these files?"}]
        self.assertEqual(self.ask(messages).message.content, "opened: /srv/shots/front.PNG")

    def test_a_changed_conversation_is_rebuilt_in_a_new_session_with_old_tool_results_whole(self):
        self.brain = lambda agy, text: (agy.call_tool("lookup", {"city": "Tokyo"}), agy.say("Noted."))
        messages = [SYSTEM, {"role": "user", "content": "Weather in Tokyo?"}]
        call = self.ask(messages).message.tool_calls[0]
        report = "day 1: mild\n" + "\n".join(f"day {n}: reading {n * 37 % 101}" for n in range(2, 3000))
        messages.append({"role": "tool", "tool_call_id": call.id, "content": report})
        self.ask(messages)

        # The user edits their first message: agy cannot rewind, so the kept turns go to a new session.
        messages[1] = {"role": "user", "content": "Weather in Kyoto?"}
        messages.append({"role": "user", "content": "What was day 1500?"})
        self.brain = lambda agy, text: agy.say("day 1500: reading 51" if "day 1500: reading 51\n" in text else "that day is gone")
        self.assertEqual(self.ask(messages).message.content, "day 1500: reading 51")
        self.assertEqual(len(self.spawned), 2)

    def test_a_silent_agy_fails_the_request_instead_of_hanging_and_the_next_one_recovers(self):
        os.environ["ANTIGRAVITY_IDLE_TIMEOUT_SECONDS"] = "0.3"
        self.brain = lambda agy, text: time.sleep(3)
        messages = [SYSTEM, {"role": "user", "content": "Weather in Tokyo?"}]
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.ask(list(messages))
        self.assertLess(time.monotonic() - started, 2)

        self.brain = self.weather_brain
        self.assertEqual(self.ask(messages).message.tool_calls[0].function.name, "lookup")

    def test_expanded_batch_tool_call_returns_every_split_result_in_order(self):
        batch_tools = [{"type": "function", "function": {
            "name": "tool_call", "description": "Invoke deferred tools.",
            "parameters": {"type": "object", "properties": {"calls": {"type": "array"}}, "required": ["calls"]},
        }}]

        def brain(agy: _Agy, text: str) -> None:
            if "Check status" in text:
                raw = agy.call_tool("tool_call", {"calls": [
                    {"name": "group_list", "arguments": {}},
                    {"name": "mesh_machines", "arguments": {}},
                ]})
                parsed = json.loads(raw)
                items = parsed.get("results") if isinstance(parsed, dict) else None
                if isinstance(items, list) and len(items) == 2:
                    agy.say(f"{items[0]['name']}={items[0]['result']['groups'][0]} {items[1]['name']}={items[1]['result']}")
                else:
                    agy.say("missing:" + raw)
            else:
                agy.say("heard: " + text)

        self.brain = brain
        messages: list[dict[str, Any]] = [SYSTEM, {"role": "user", "content": "Check status"}]
        first = self.ask(messages, tools=batch_tools)
        self.assertEqual(first.finish_reason, "tool_calls")
        call = first.message.tool_calls[0]
        # Host expand_local_tool_batches splits one batch call into one entry per child:
        # index 0 keeps call.id and later entries get new ids.
        messages[-1]["tool_calls"] = [
            {"id": call.id, "type": "function", "function": {
                "name": "tool_call", "arguments": json.dumps({"calls": [{"name": "group_list", "arguments": {}}]})}
            },
            {"id": "call_expanded_2", "type": "function", "function": {
                "name": "tool_call", "arguments": json.dumps({"calls": [{"name": "mesh_machines", "arguments": {}}]})}
            },
        ]
        # Append out of emitted order to verify the connector restores assistant call order.
        messages.append({"role": "tool", "tool_call_id": "call_expanded_2", "name": "mesh_machines",
                         "content": "laptop-1 online"})
        messages.append({"role": "tool", "tool_call_id": call.id, "name": "group_list",
                         "content": json.dumps({"groups": ["Inbox"]})})

        second = self.ask(messages, tools=batch_tools)
        self.assertEqual((second.finish_reason, second.message.content),
                         ("stop", "group_list=Inbox mesh_machines=laptop-1 online"))
        self.assertEqual(len(self.spawned), 1)

        messages.append({"role": "user", "content": "All good?"})
        third = self.ask(messages, tools=batch_tools)
        self.assertEqual(third.message.content, "heard: All good?")
        self.assertEqual(len(self.spawned), 1)

    def test_expanded_batch_missing_a_split_result_rebuilds_session(self):
        batch_tools = [{"type": "function", "function": {
            "name": "tool_call", "description": "Invoke deferred tools.",
            "parameters": {"type": "object", "properties": {"calls": {"type": "array"}}, "required": ["calls"]},
        }}]

        def brain(agy: _Agy, text: str) -> None:
            if "CONVERSATION SO FAR" in text:
                agy.say("rebuilt")
            else:
                agy.say("same:" + agy.call_tool("tool_call", {"calls": [
                    {"name": "group_list", "arguments": {}},
                    {"name": "mesh_machines", "arguments": {}},
                ]}))

        self.brain = brain
        messages: list[dict[str, Any]] = [SYSTEM, {"role": "user", "content": "Check status"}]
        call = self.ask(messages, tools=batch_tools).message.tool_calls[0]
        messages[-1]["tool_calls"] = [
            {"id": call.id, "type": "function", "function": {
                "name": "tool_call", "arguments": json.dumps({"calls": [{"name": "group_list", "arguments": {}}]})}
            },
            {"id": "call_expanded_2", "type": "function", "function": {
                "name": "tool_call", "arguments": json.dumps({"calls": [{"name": "mesh_machines", "arguments": {}}]})}
            },
        ]
        # Only child 0's result is present; child 1 was skipped.
        messages.append({"role": "tool", "tool_call_id": call.id, "name": "group_list",
                         "content": json.dumps({"groups": ["Inbox"]})})

        second = self.ask(messages, tools=batch_tools)
        self.assertEqual(second.message.content, "rebuilt")
        self.assertEqual(len(self.spawned), 2)


if __name__ == "__main__":
    unittest.main()

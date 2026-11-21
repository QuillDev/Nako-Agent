"""Stdio MCP server agy launches for the host's tools: a line relay to the plugin's tool bridge.

agy speaks MCP to this process over stdin/stdout; the bridge that answers lives inside the host
(``native.ToolBridge``), so every line is passed through a local socket unchanged.
"""

from __future__ import annotations

import os
import socket
import sys
import threading


def main() -> None:
    host, _, port = os.environ["ANTIGRAVITY_BRIDGE_ADDR"].rpartition(":")
    sock = socket.create_connection((host, int(port)))
    sock.sendall((os.environ["ANTIGRAVITY_BRIDGE_TOKEN"] + "\n").encode("utf-8"))

    def upstream() -> None:
        try:
            for line in sys.stdin.buffer:
                sock.sendall(line)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=upstream, daemon=True).start()
    with sock.makefile("rb") as replies:
        for line in replies:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()

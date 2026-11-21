"""Local OpenAI-compatible proxy that forwards to OAuth-authenticated upstreams."""

from nako_cli.proxy.adapters.base import UpstreamAdapter

__all__ = ["UpstreamAdapter"]

"""Tests for the Nous-Nako-3/4 non-agentic warning detector.

Prior to this check, the warning fired on any model whose name contained
``"nako"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``nako-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under the "nako" tag namespace.

``is_nous_nako_non_agentic`` should only match the actual Quill Dev
Nako-3 / Nako-4 chat family.
"""

from __future__ import annotations

import pytest

from nako_cli.model_switch import (
    _NAKO_MODEL_WARNING,
    _check_nako_model_warning,
    is_nous_nako_non_agentic,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "NousResearch/Nako-3-Llama-3.1-70B",
        "NousResearch/Nako-3-Llama-3.1-405B",
        "nako-3",
        "Nako-3",
        "nako-4",
        "nako-4-405b",
        "nako_4_70b",
        "openrouter/nako3:70b",
        "openrouter/nousresearch/nako-4-405b",
        "NousResearch/Nako3",
        "nako-3.1",
    ],
)
def test_matches_real_nous_nako_chat_models(model_name: str) -> None:
    assert is_nous_nako_non_agentic(model_name), (
        f"expected {model_name!r} to be flagged as Nous Nako 3/4"
    )
    assert _check_nako_model_warning(model_name) == _NAKO_MODEL_WARNING



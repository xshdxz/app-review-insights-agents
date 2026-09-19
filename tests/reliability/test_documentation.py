"""文档一致性：docs/reliability.md 列出的场景必须与代码枚举一致。

一份会漂移的可靠性矩阵还不如没有——它会让读者以为覆盖了实际没测的东西。
两个方向都要查：代码里有而文档没写、文档里有而代码已删。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import scenario

DOC = Path(__file__).resolve().parents[2] / "docs" / "reliability.md"

#: 文档里场景 ID 的形态：写在反引号里的 "stage:xxx" / "batch:n" / "kill:xxx"
_ID_PATTERN = re.compile(r"`((?:stage|batch|kill):[^`]+)`")


@pytest.mark.reliability
def test_every_scenario_is_documented():
    text = DOC.read_text(encoding="utf-8")

    missing = [scenario_id for scenario_id in scenario.scenario_ids() if scenario_id not in text]

    assert not missing, (
        f"以下场景没有写进 docs/reliability.md：{missing}。"
        "加了场景就要同步文档，否则矩阵看起来覆盖了实际没测的东西。"
    )


@pytest.mark.reliability
def test_document_lists_no_scenario_that_no_longer_exists():
    documented = set(_ID_PATTERN.findall(DOC.read_text(encoding="utf-8")))
    known = set(scenario.scenario_ids())

    stale = sorted(documented - known)
    assert not stale, (
        f"docs/reliability.md 里这些场景已经不在代码里了：{stale}。"
        "删场景的时候把文档一起改掉，否则读者会以为它还在跑。"
    )

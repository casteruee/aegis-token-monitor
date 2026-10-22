"""Pytest 配置（项目级）。

注册 ``@pytest.mark.live`` 自定义标记，消除
``PytestUnknownMarkWarning``。``live`` 标记表示「真机依赖测试」，
默认情况下仍会运行；若 CodexBar / 凭据不可用则测试自带的
``@pytest.mark.skipif`` 会跳过该条。

约定（与 docs/tasks/pi_P*.md「测试要求」一致）：
- 离线默认全绿（无网络、无真机凭据）。
- 真机调用单独标 ``@pytest.mark.live``，由 ``pytest -m 'not live'`` 显式
  排除，或在真机环境直接跑通。
"""

from __future__ import annotations


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: 真机依赖测试（需 CodexBar / 凭据 / 网络）。默认运行；"
        "在 CI 或无真机环境用 `pytest -m 'not live'` 排除。",
    )
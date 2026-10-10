"""回归守卫：热路径不得再调用会走 WMI 的 ``platform.platform()``。

``platform.platform()`` 在 Windows 上走 ``win32_ver()`` -> ``_wmi_query``（WMI）。
WMI 服务退化时该调用可挂死数分钟，把 cycle / backfill 拖成"像是卡住"
（2026-10-10 经 faulthandler 栈转储定性）。环境标签改由
``prediction_research.runtime_env.platform_label`` 提供（只用 sys / os.environ）。
"""

import ast
import sys
from pathlib import Path

from prediction_research import backfill, cycle
from prediction_research.runtime_env import platform_label

_WMI_BOUND_PLATFORM_ATTRS = {"platform", "uname", "win32_ver", "system", "release", "machine"}


def _platform_module_calls(module) -> list[tuple[int, str]]:
    """Return ``(lineno, attr)`` for every real ``platform.<attr>(...)`` call.

    AST 只看真实调用，注释与文档字符串里的 "platform.platform()" 不会误报。
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _WMI_BOUND_PLATFORM_ATTRS:
            continue
        value = node.func.value
        if isinstance(value, ast.Name) and value.id == "platform":
            hits.append((node.lineno, node.func.attr))
    return hits


def test_cycle_and_backfill_do_not_call_wmi_bound_platform_functions():
    assert _platform_module_calls(cycle) == []
    assert _platform_module_calls(backfill) == []


def test_platform_label_shape():
    label = platform_label()
    assert label.startswith(sys.platform)
    info = sys.version_info
    assert f"python{info.major}.{info.minor}.{info.micro}" in label


# --- 进程级 WMI 守卫 -------------------------------------------------------
# 仅改 runtime_env 还不够：``pandas.compat._constants`` 在**导入期**就调用
# ``platform.machine()`` -> ... -> ``_wmi_query``，所以「任何 import pandas」
# 都会触 WMI（2026-10-10 实测：test_screen_handoff 因此挂死 90s）。
# wmi_guard 在 import 时把 ``platform._wmi`` 置 None，使所有 WMI 探测立即
# 以 OSError 短路并回退到非 WMI 源。


def test_wmi_guard_clears_platform_wmi_handle():
    import platform

    import wmi_guard  # noqa: F401

    # 非 Windows 上 ``_wmi`` 本就不存在，getattr 兜底同样应为 None。
    assert getattr(platform, "_wmi", None) is None


def test_platform_machine_does_not_hang():
    import platform
    import time

    start = time.monotonic()
    value = platform.machine()
    elapsed = time.monotonic() - start
    # WMI 退化时旧路径会阻塞数分钟；守卫生效后应毫秒级返回。
    assert elapsed < 5.0, f"platform.machine() took {elapsed:.1f}s"
    assert isinstance(value, str)


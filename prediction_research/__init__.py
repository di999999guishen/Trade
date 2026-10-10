"""Isolated, point-in-time ETF prediction research core."""

# 🔧 WMI 守卫（实现在仓库根 wmi_guard.py）。放在包 __init__ 里，任何
# ``import prediction_research.*`` 都会先经过这里，早于 pandas 的导入期
# ``platform.machine()`` 调用，避免 WMI 服务退化时挂死数分钟。
# 根目录不在 sys.path 时退化为就地清理。
try:
    from wmi_guard import disable_wmi as _disable_wmi
except Exception:  # pragma: no cover - 仅兜底
    import platform as _platform

    def _disable_wmi() -> bool:
        if hasattr(_platform, "_wmi"):
            _platform._wmi = None
            return True
        return False

_disable_wmi()

__version__ = "0.1.0"

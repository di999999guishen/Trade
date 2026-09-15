"""ETF 研究预测流程入口（本环境专用包装）

对 prediction_research 模块的命令行入口做代理绕过与 .env 加载，
使 `python -m prediction_research.cli ...` 在本机（全局代理开启）环境下
也能直连东方财富/新浪等国内数据源，不经过限流代理。

推荐用法（等价于 USAGE.md 的日常命令）：

    python run_research.py cycle --top 5
    python run_research.py status
    python run_research.py doctor

其余子命令（fetch-etfs / screen-etfs / backtest / predict / settle / report /
run-agents 等）原样透传给 prediction_research.cli。
"""

import sys
from pathlib import Path

# 项目根加入 sys.path，保证 retry_utils 与 prediction_research 都可导入
PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

# 🔧 代理绕过（必须在所有网络请求之前）
try:
    from retry_utils import setup_proxy_bypass

    setup_proxy_bypass()
except Exception:  # pragma: no cover - retry_utils 缺失时不阻断
    pass

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_DIR / ".env")
except Exception:
    pass

from prediction_research.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

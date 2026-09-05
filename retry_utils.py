"""
网络重试 + 代理绕过工具模块
植入到 morning_briefing.py / daily_predict.py / fetcher.py 中确保网络中断不崩溃
"""
import os
import time
import functools
import logging
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

# ── 需要绕过全局代理直连的域名 ──
BYPASS_DOMAINS = [
    "api.deepseek.com",
    "push2.eastmoney.com",
    "eastmoney.com",
    "yahoo.com",
    "api.query.yahoo.com",
    "fc.yahoo.com",
    "query1.finance.yahoo.com",
    "query2.finance.yahoo.com",
    "datacenter.eastmoney.com",
    "push2delay.eastmoney.com",
    # Sina / Tencent finance APIs for domestic stock data
    "sina.com.cn",
    "sina.com",
    "sina.cn",
    "finance.sina.com.cn",
    "money.finance.sina.com.cn",
    "hq.sinajs.cn",
    "qt.gtimg.cn",
    "web.ifzq.gtimg.cn",
    "gtimg.cn",
    "gtimg.com",
    "qq.com",
]

# ── 网络错误类型（跨库通用） ──
NETWORK_ERROR_PATTERNS = [
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
    "RemoteDisconnected", "ProtocolError", "ProxyError",
    "ConnectionRefused", "ConnectionReset", "ConnectionAborted",
    "connect timeout", "read timeout", "timed out",
    "EOF occurred", "SSLError", "TunnelError",
    "TooManyRedirects", "ChunkedEncodingError",
    "IncompleteRead", "BadStatusLine",
    "ServiceUnavailable", "ServerDisconnected",
    "ECONNREFUSED", "ECONNRESET", "ETIMEDOUT",
]

T = TypeVar("T")


def is_network_error(exc: Exception) -> bool:
    """判断异常是否为网络相关（支持任意库抛出的异常）"""
    exc_str = str(exc)
    exc_repr = repr(exc)
    combined = f"{exc_str} {exc_repr}".lower()
    for pattern in NETWORK_ERROR_PATTERNS:
        if pattern.lower() in combined:
            return True
    # 检查异常链
    while exc.__cause__:
        exc = exc.__cause__
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        for pattern in NETWORK_ERROR_PATTERNS:
            if pattern.lower() in name or pattern.lower() in msg:
                return True
    return False


def setup_proxy_bypass(extra_domains: list[str] | None = None):
    """
    强制设置环境变量，让关键 API 域名绕过全局代理。
    必须在所有网络请求之前调用，且会覆盖 WorkBuddy session 注入的值。
    """
    domains = BYPASS_DOMAINS + (extra_domains or [])
    bypass_str = ",".join(domains)

    for key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(key, "")
        if not existing or existing in ("localhost,127.0.0.1,::1", ""):
            # WorkBuddy 默认值，直接替换
            os.environ[key] = f"localhost,127.0.0.1,::1,{bypass_str}"
        else:
            # 追加缺失的域名
            missing = [d for d in domains if d not in existing]
            if missing:
                os.environ[key] = f"{existing},{','.join(missing)}"

    # 同时设置 HTTP/HTTPS 层面的代理绕过（urllib3 需要大写）
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        # 如果 .env 里也设了代理，保留（用户可能在 env 里配置了）
        pass

    logger.debug("Proxy bypass configured for: %s", bypass_str)


def retry_on_network_error(
    max_retries: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    on_retry: Callable[[Exception, int], None] | None = None,
):
    """
    装饰器：在网络错误时自动重试，指数退避。

    Args:
        max_retries: 最大重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟上限（秒）
        backoff_factor: 退避倍数
        on_retry: 重试时的回调函数，接收 (exception, attempt_number)
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if not is_network_error(e):
                        raise  # 非网络错误，立即抛出

                    if attempt < max_retries:
                        delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                        func_name = getattr(func, "__name__", str(func))
                        logger.warning(
                            "[重试 %d/%d] %s 因网络错误失败，%ds 后重试: %s",
                            attempt + 1, max_retries, func_name, delay, str(e)[:100]
                        )
                        if on_retry:
                            on_retry(e, attempt + 1)
                        time.sleep(delay)
                    else:
                        logger.error(
                            "[全部重试耗尽] %s 在 %d 次重试后仍然失败: %s",
                            getattr(func, "__name__", str(func)),
                            max_retries, str(e)[:200]
                        )
            raise last_exc
        return wrapper
    return decorator


class RetryContext:
    """
    上下文管理器：在 with 块内自动重试网络错误。

    Usage:
        with RetryContext("fetch_news", max_retries=3) as retry:
            data = some_network_call()
            if retry.last_error:
                handle_partial()
    """

    def __init__(
        self,
        name: str = "operation",
        max_retries: int = 3,
        base_delay: float = 2.0,
        max_delay: float = 30.0,
    ):
        self.name = name
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.last_error: Exception | None = None
        self.attempt = 0

    def __enter__(self):
        self.last_error = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is None:
            return False  # 正常退出

        self.last_error = exc_val
        self.attempt += 1

        if not is_network_error(exc_val):
            return False  # 非网络错误，不抑制

        if self.attempt <= self.max_retries:
            delay = min(self.base_delay * (2 ** (self.attempt - 1)), self.max_delay)
            logger.warning(
                "[重试 %d/%d] %s 因网络错误失败，%ds 后重试: %s",
                self.attempt, self.max_retries, self.name, delay, str(exc_val)[:100]
            )
            time.sleep(delay)
            return True  # 抑制异常，重试

        logger.error(
            "[全部重试耗尽] %s 在 %d 次重试后仍然失败: %s",
            self.name, self.max_retries, str(exc_val)[:200]
        )
        return False  # 重试耗尽，再次抛出

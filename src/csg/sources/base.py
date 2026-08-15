"""数据源调用的容错基础设施。

akshare 大量接口以爬取公开站点实现，实测故障是常态而非例外：
本项目开发期间就遇到深交所 SSL 握手失败、东财 RemoteDisconnected 限流。
因此**必须假设任一接口随时失效**，容错不是锦上添花。

三条策略：
1. 指数退避重试 —— 应对瞬时限流
2. 全局最小请求间隔 —— 主动降速，避免触发限流
3. 多接口 fallback —— 同一份数据换个来源再试
"""

from __future__ import annotations

import logging
import random
import socket
import time
from collections.abc import Callable, Sequence
from typing import TypeVar

import pandas as pd

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 全局 socket 超时 —— 防止请求无限挂起
# ----------------------------------------------------------------------
#
# 实测教训：一次研报采集在 375/604 处挂死，进程存活但两小时仅消耗
# 21 秒 CPU。akshare 部分接口未设 timeout，底层 socket 会无限等待。
#
# 重试逻辑对此**无效**——它只能捕获抛出的异常，无法处理「永不返回」。
# 这正是架构文档所述的「静默失效」：系统看起来在跑，实际早已停止且不报错。
#
# socket.setdefaulttimeout 影响本进程所有 socket 操作，
# 使挂起转化为可捕获的异常，重试与熔断机制才能真正生效。
SOCKET_TIMEOUT = 45.0
socket.setdefaulttimeout(SOCKET_TIMEOUT)

T = TypeVar("T")

# 网络类异常统称。akshare 会把底层异常原样抛出，类型繁杂，
# 因此按异常名匹配而非类型匹配。
_RETRYABLE_NAMES = {
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "Timeout",
    "timeout",          # socket.timeout：全局超时触发时抛出
    "TimeoutError",
    "SSLError",
    "ChunkedEncodingError",
    "RemoteDisconnected",
    "ProtocolError",
    "HTTPError",
    "JSONDecodeError",
    "IncompleteRead",
}


def _is_retryable(exc: BaseException) -> bool:
    names = {type(exc).__name__}
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        names.add(type(cause).__name__)
    return bool(names & _RETRYABLE_NAMES)


class RateLimiter:
    """全局最小请求间隔 + 连续失败熔断。

    东财在连续请求下会直接断开连接。主动降速比事后重试更有效，
    因为触发限流后的恢复时间远长于主动等待。

    默认 1.5s 是实测值：开发期以 0.6s 间隔连续探测约二十次后，
    东财对所有接口持续返回 RemoteDisconnected，需静置数分钟才恢复。
    """

    def __init__(
        self,
        min_interval: float = 1.5,
        *,
        cooldown_after: int = 6,
        cooldown_secs: float = 60.0,
    ) -> None:
        self.min_interval = min_interval
        self.cooldown_after = cooldown_after
        self.cooldown_secs = cooldown_secs
        self._last = 0.0
        self._consecutive_failures = 0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """连续失败达阈值即长时间静置。

        被限流后继续以指数退避重试仍会持续踩线，反而延长封禁。
        """
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.cooldown_after:
            log.warning(
                "连续失败 %d 次，判定为限流，静置 %.0f 秒",
                self._consecutive_failures,
                self.cooldown_secs,
            )
            time.sleep(self.cooldown_secs)
            self._consecutive_failures = 0


_limiter = RateLimiter()


def call(
    fn: Callable[..., T],
    *args,
    retries: int = 4,
    base_delay: float = 1.5,
    limiter: RateLimiter | None = None,
    **kwargs,
) -> T:
    """带限速与指数退避重试的调用。

    非网络类异常（参数错误、字段缺失）立即抛出 —— 重试不会让它变好，
    只会浪费时间并掩盖真实错误。
    """
    lim = limiter or _limiter
    last_exc: BaseException | None = None

    for attempt in range(retries):
        lim.wait()
        try:
            result = fn(*args, **kwargs)
            lim.record_success()
            return result
        except Exception as exc:  # noqa: BLE001 — 需按异常名分流
            if not _is_retryable(exc):
                raise
            last_exc = exc
            lim.record_failure()
            if attempt == retries - 1:
                break
            # 抖动避免多任务同时重试形成新的峰值
            delay = base_delay * (2**attempt) + random.uniform(0, 0.8)
            log.warning(
                "%s 第 %d/%d 次失败(%s)，%.1fs 后重试",
                getattr(fn, "__name__", fn),
                attempt + 1,
                retries,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"{getattr(fn, '__name__', fn)} 重试 {retries} 次仍失败"
    ) from last_exc


def first_success(
    candidates: Sequence[tuple[str, Callable[[], pd.DataFrame]]],
    *,
    retries: int = 2,
) -> tuple[str, pd.DataFrame]:
    """依次尝试多个等价接口，返回首个成功的 (来源名, 数据)。

    用于同一份数据存在多个来源的情形（如股票列表在交易所与东财各有接口）。
    单一来源失效时自动切换，而不是让整条管线停摆。
    """
    errors: list[str] = []
    for name, fetch in candidates:
        try:
            df = call(fetch, retries=retries)
            if df is not None and len(df) > 0:
                log.info("数据源 %s 成功，%d 行", name, len(df))
                return name, df
            errors.append(f"{name}: 空返回")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}")
            log.warning("数据源 %s 不可用: %s", name, exc)

    raise RuntimeError("所有候选数据源均失败 -> " + "; ".join(errors))

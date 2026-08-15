"""DuckDB 存储层。

内存约束（见 docs/ARCHITECTURE.md 4.1）：目标机器仅 8 GB 内存。
本模块默认限制 DuckDB 用量并启用磁盘溢写，所有聚合在 SQL 内完成，
调用方只接收结果集。禁止 `SELECT *` 后在 pandas 里做全量计算。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from pathlib import Path

import duckdb
import pandas as pd

SCHEMA_PATHS = (
    Path(__file__).parent / "schema.sql",         # 数据层
    Path(__file__).parent / "schema_events.sql",  # 事件层 + 验证结果
)
DEFAULT_DB = Path("data/csg.duckdb")

# 8 GB 物理内存：留给 macOS 与 pandas，DuckDB 上限设 4 GB，超出部分溢写磁盘。
DEFAULT_MEMORY_LIMIT = "4GB"


class Database:
    """DuckDB 连接封装。

    Point-in-Time 查询以方法形式提供，不鼓励调用方手写 SQL——
    用 report_period 代替 disclosure_date 过滤是最容易犯且最难发现的错误，
    它不会报错，只会让回测收益凭空变好。
    """

    def __init__(
        self,
        path: Path | str = DEFAULT_DB,
        *,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        read_only: bool = False,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = duckdb.connect(str(self.path), read_only=read_only)
        if not read_only:
            self.conn.execute(f"SET memory_limit='{memory_limit}'")
            # 溢写目录与数据库同盘，磁盘水位由健康检查监控
            temp_dir = self.path.parent / "duckdb_tmp"
            temp_dir.mkdir(exist_ok=True)
            self.conn.execute(f"SET temp_directory='{temp_dir}'")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        for path in SCHEMA_PATHS:
            if path.exists():
                self.conn.execute(path.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------

    def query(self, sql: str, params: Sequence | None = None) -> pd.DataFrame:
        return self.conn.execute(sql, params or []).df()

    def upsert(self, table: str, df: pd.DataFrame, keys: Sequence[str]) -> int:
        """按主键覆盖写入。空 DataFrame 直接返回，便于调用方无脑调用。

        幂等要求（见 ARCHITECTURE.md 4.3）：重复写入同一批数据不产生重复行，
        因此采集任务可以安全重跑，中断后从水位继续即可。
        """
        if df.empty:
            return 0

        cols = list(df.columns)
        self.conn.register("_incoming", df)
        try:
            key_match = " AND ".join(f"t.{k} = s.{k}" for k in keys)
            self.conn.execute(
                f"DELETE FROM {table} t USING _incoming s WHERE {key_match}"
            )
            self.conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) "
                f"SELECT {', '.join(cols)} FROM _incoming"
            )
        finally:
            self.conn.unregister("_incoming")
        return len(df)

    # ------------------------------------------------------------------
    # 水位（幂等自愈）
    # ------------------------------------------------------------------

    def get_watermark(self, dataset: str, scope: str = "__ALL__") -> dt.date | None:
        row = self.conn.execute(
            "SELECT last_success_date FROM sync_watermark "
            "WHERE dataset = ? AND scope = ?",
            [dataset, scope],
        ).fetchone()
        return row[0] if row else None

    def set_watermark(
        self,
        dataset: str,
        scope: str = "__ALL__",
        *,
        last_success_date: dt.date | None = None,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "DELETE FROM sync_watermark WHERE dataset = ? AND scope = ?",
            [dataset, scope],
        )
        self.conn.execute(
            "INSERT INTO sync_watermark "
            "(dataset, scope, last_success_date, last_run_at, status, error) "
            "VALUES (?, ?, ?, current_timestamp, ?, ?)",
            [dataset, scope, last_success_date, status, error],
        )

    def stale_scopes(self, dataset: str, days: int) -> pd.DataFrame:
        """水位落后超过 N 天的条目 —— 调度静默失效的检测手段。

        这类失败没有报错：系统「看起来在跑」，实际上早就停了。
        """
        return self.query(
            "SELECT scope, last_success_date, status, error FROM sync_watermark "
            "WHERE dataset = ? AND (last_success_date IS NULL "
            "   OR last_success_date < current_date - ?) "
            "ORDER BY last_success_date NULLS FIRST",
            [dataset, days],
        )

    # ------------------------------------------------------------------
    # Point-in-Time 查询
    # ------------------------------------------------------------------

    def pit_universe(self, as_of: dt.date, *, exclude_st: bool = False) -> pd.DataFrame:
        """`as_of` 当日真实在市的股票列表。

        含此后已退市的公司 —— 这正是消除幸存者偏差的关键。
        用今天的股票列表回测历史，会自动剔除所有退市股，凭空美化收益。
        """
        sql = """
            SELECT b.code, b.name, b.market, b.list_date, b.delist_date
            FROM stock_basic b
            WHERE b.list_date <= ?
              AND (b.delist_date IS NULL OR b.delist_date > ?)
        """
        params: list = [as_of, as_of]

        if exclude_st:
            # ST 状态按当时的名称判断，不能用当前名称
            sql += """
              AND NOT EXISTS (
                    SELECT 1 FROM stock_name_history h
                    WHERE h.code = b.code AND h.is_st
                      AND h.start_date <= ?
                      AND (h.end_date IS NULL OR h.end_date > ?)
              )
            """
            params += [as_of, as_of]

        return self.query(sql, params)

    def pit_financials(
        self,
        as_of: dt.date,
        *,
        table: str = "fin_income",
        codes: Iterable[str] | None = None,
        lookback_periods: int = 1,
    ) -> pd.DataFrame:
        """`as_of` 时点**已经披露**的最近 N 期财报。

        以 disclosure_date 过滤，不是 report_period —— 这是 PIT 的全部要害。
        2024Q3 报告期的数据可能到 2024-10-28 才公布；在 10-28 之前使用它
        就是未来函数，回测会凭空变好且无法在结果里看出来。
        """
        if table not in {"fin_income", "fin_balance", "fin_cashflow"}:
            raise ValueError(f"不支持的财务表: {table}")

        sql = f"""
            SELECT * FROM (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY code ORDER BY report_period DESC
                       ) AS _rn
                FROM {table}
                WHERE disclosure_date IS NOT NULL
                  AND disclosure_date <= ?
                  {{code_filter}}
            ) WHERE _rn <= ?
        """
        params: list = [as_of]

        if codes is not None:
            code_list = list(codes)
            if not code_list:
                return pd.DataFrame()
            placeholders = ", ".join("?" * len(code_list))
            sql = sql.replace("{code_filter}", f"AND code IN ({placeholders})")
            params += code_list
        else:
            sql = sql.replace("{code_filter}", "")

        params.append(lookback_periods)
        return self.query(sql, params).drop(columns=["_rn"])

    def adjusted_close(
        self,
        codes: Iterable[str],
        start: dt.date,
        end: dt.date,
        *,
        adjust: str = "hfq",
    ) -> pd.DataFrame:
        """复权收盘价。

        后复权（hfq）用于计算收益率与技术指标：它不会因未来的除权事件
        改变历史值。前复权会——所以前复权价绝不能落盘，只在展示时计算。
        """
        code_list = list(codes)
        if not code_list:
            return pd.DataFrame()

        price_expr = {
            "hfq": "close * adj_factor",
            "none": "close",
        }.get(adjust)
        if price_expr is None:
            raise ValueError(f"adjust 仅支持 hfq / none，收到 {adjust!r}")

        placeholders = ", ".join("?" * len(code_list))
        return self.query(
            f"""
            SELECT code, trade_date, {price_expr} AS px, volume, amount
            FROM daily_quote
            WHERE code IN ({placeholders})
              AND trade_date BETWEEN ? AND ?
            ORDER BY code, trade_date
            """,
            [*code_list, start, end],
        )


def open_db(path: Path | str = DEFAULT_DB, **kwargs) -> Database:
    """打开数据库；非只读时确保 schema 就绪。

    只读连接不能执行 CREATE，故跳过建表——只读场景下库必然已存在。
    """
    db = Database(path, **kwargs)
    if not kwargs.get("read_only"):
        db.init_schema()
    return db

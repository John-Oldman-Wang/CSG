"""验证结果持久化。

回测结果必须落库而非每次重算：
1. 回测耗时，且结果依赖当时的数据状态——数据在增长，同一段代码
   下个月跑出的数字会不同，不记录快照就无法解释差异
2. 结论会被后续决策规则引用（如「评级下调」事件的严重度权重），
   必须可追溯到具体的运行与数据
3. 参数调整后需要横向对比，而非覆盖旧结果

**防过拟合的落库纪律**：结论表要求同时填写发现期与验证期结果，
`verdict = adopted` 仅在两期同向时允许。被证伪的想法同样保留——
记住哪些假设失败过，与记住哪些成立同样重要。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import pandas as pd

from csg.storage import Database


def _run_id(validation_type: str, params: dict) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    digest = hashlib.sha1(
        json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:6]
    return f"{validation_type}_{stamp}_{digest}"


def snapshot_data_state(db: Database) -> dict:
    """记录当前数据规模 —— 解释结果差异的依据。

    同一段代码在数据增长后会跑出不同数字，没有快照就无法判断
    差异来自代码变更还是数据变更。
    """
    def scalar(sql: str) -> Any:
        try:
            v = db.query(sql).iloc[0, 0]
            return None if pd.isna(v) else (
                str(v) if isinstance(v, (pd.Timestamp, dt.date)) else int(v)
            )
        except Exception:  # noqa: BLE001 — 表可能尚未建立
            return None

    return {
        "research_reports": scalar("SELECT count(*) FROM research_report"),
        "research_codes": scalar("SELECT count(DISTINCT code) FROM research_report"),
        "report_earliest": scalar("SELECT min(publish_date) FROM research_report"),
        "report_latest": scalar("SELECT max(publish_date) FROM research_report"),
        "quote_rows": scalar("SELECT count(*) FROM daily_quote"),
        "quote_codes": scalar("SELECT count(DISTINCT code) FROM daily_quote"),
        "quote_earliest": scalar("SELECT min(trade_date) FROM daily_quote"),
        "quote_latest": scalar("SELECT max(trade_date) FROM daily_quote"),
        "fin_rows": scalar("SELECT count(*) FROM fin_income"),
    }


def save_run(
    db: Database,
    validation_type: str,
    params: dict,
    results: dict[str, pd.DataFrame],
    *,
    note: str = "",
) -> str:
    """保存一次验证运行及其全部结果视角。返回 run_id。

    `results` 的键形如「发现期_评级」「验证期_评级调整」，
    在此拆解为 (sample_period, view_name) 两个维度落库，
    便于后续按视角跨期对照。
    """
    params = dict(params)
    rid = _run_id(validation_type, params)

    db.conn.execute(
        "INSERT INTO validation_run "
        "(run_id, validation_type, params, data_snapshot, status, note) "
        "VALUES (?, ?, ?, ?, 'ok', ?)",
        [rid, validation_type,
         json.dumps(params, ensure_ascii=False, default=str),
         json.dumps(snapshot_data_state(db), ensure_ascii=False, default=str),
         note],
    )

    rows: list[dict] = []
    for key, table in results.items():
        if table is None or table.empty:
            continue
        sample_period, _, view_name = key.partition("_")
        if not view_name:
            sample_period, view_name = "全期", key

        # 首列约定为分组名，其余为指标
        group_col = table.columns[0]
        for _, r in table.iterrows():
            metrics = {
                c: (None if pd.isna(r[c]) else
                    float(r[c]) if isinstance(r[c], (int, float)) else str(r[c]))
                for c in table.columns if c != group_col
            }
            rows.append({
                "run_id": rid,
                "view_name": view_name,
                "sample_period": sample_period,
                "group_key": str(r[group_col]),
                "sample_size": int(r["样本数"]) if "样本数" in table.columns
                               and pd.notna(r["样本数"]) else None,
                "metrics": json.dumps(metrics, ensure_ascii=False),
            })

    if rows:
        db.upsert("validation_result", pd.DataFrame(rows),
                  ["run_id", "view_name", "sample_period", "group_key"])
    return rid


def load_run(db: Database, run_id: str) -> dict[str, pd.DataFrame]:
    """按 run_id 还原结果，结构与保存时一致。"""
    df = db.query(
        "SELECT view_name, sample_period, group_key, sample_size, metrics "
        "FROM validation_result WHERE run_id = ? "
        "ORDER BY sample_period, view_name",
        [run_id],
    )
    out: dict[str, pd.DataFrame] = {}
    for (period, view), grp in df.groupby(["sample_period", "view_name"]):
        recs = []
        for _, r in grp.iterrows():
            rec = {"分组": r["group_key"], "样本数": r["sample_size"]}
            rec.update(json.loads(r["metrics"]))
            recs.append(rec)
        out[f"{period}_{view}"] = pd.DataFrame(recs)
    return out


def list_runs(db: Database, validation_type: str | None = None) -> pd.DataFrame:
    sql = """
        SELECT r.run_id, r.validation_type, r.run_at, r.note,
               json_extract_string(r.data_snapshot, '$.research_reports') AS 研报数,
               json_extract_string(r.data_snapshot, '$.quote_rows')       AS 行情行数,
               count(v.group_key) AS 结果行数
        FROM validation_run r
        LEFT JOIN validation_result v ON v.run_id = r.run_id
    """
    params: list = []
    if validation_type:
        sql += " WHERE r.validation_type = ?"
        params.append(validation_type)
    sql += " GROUP BY ALL ORDER BY r.run_at DESC"
    return db.query(sql, params)


def save_conclusion(
    db: Database,
    *,
    validation_type: str,
    finding: str,
    in_sample: str,
    out_sample: str,
    verdict: str,
    run_id: str | None = None,
    applied_to: str = "",
    note: str = "",
) -> str:
    """记录人工判定 —— 「回测发现 → 是否采纳」的桥梁。

    强制要求同时填写两期结果：`verdict='adopted'` 只应在两期同向时使用。
    被否定的发现同样保留，避免日后重复尝试同一个已被证伪的想法。
    """
    if verdict not in {"adopted", "rejected", "pending"}:
        raise ValueError("verdict 须为 adopted / rejected / pending")

    cid = hashlib.sha1(
        f"{validation_type}|{finding}|{dt.datetime.now()}".encode()
    ).hexdigest()[:16]

    db.conn.execute(
        "INSERT INTO validation_conclusion "
        "(conclusion_id, run_id, validation_type, finding, in_sample, out_sample, "
        " verdict, applied_to, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [cid, run_id, validation_type, finding, in_sample, out_sample,
         verdict, applied_to, note],
    )
    return cid

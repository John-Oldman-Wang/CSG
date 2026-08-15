#!/bin/bash
# 采集财务数据（三大报表 + PIT 披露日期）
#
# 只能走东财详细接口：baostock 拿不到商誉、在建工程、合同负债、
# 资本开支——这些正是红旗规则与行业指标最需要的字段。
#
# 全程幂等：随时 Ctrl+C，重跑自动跳过已完成的股票。
#
# ⚠️ 日志**刻意不经过管道**。
# 本项目已四次因日志不可见而误判系统状态（socket 挂起、requests 挂起、
# grep 块缓冲、日志条件写错），每次都浪费大量时间在「它是慢还是死了」上。
# tqdm 噪音改由 TQDM_DISABLE 从源头关闭，而非事后 grep 过滤——
# 过滤器本身会引入缓冲并可能误伤真正的告警行。
set -u
cd /Users/wwww/Documents/GitHub/CSG
LIMIT="${1:-0}"

echo "[$(date +%H:%M:%S)] === 财务数据采集 ${LIMIT:+（试跑 $LIMIT 只）} ==="

export TQDM_DISABLE=1
export PYTHONUNBUFFERED=1

.venv/bin/python -u - "$LIMIT" <<'PY'
import logging
import sys
import warnings

warnings.filterwarnings("ignore")
# 显式指定 stream=sys.stdout：logging 默认写 stderr，
# 与 stdout 混合重定向时顺序会错乱，且更易被缓冲。
logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

from csg.pipeline import Ingestor
from csg.storage import open_db

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
db = open_db("data/csg.duckdb")
codes = db.query(
    "SELECT DISTINCT code FROM daily_quote ORDER BY code")["code"].tolist()
if limit:
    codes = codes[:limit]

done = db.query("""
    SELECT count(*) n FROM sync_watermark
    WHERE dataset = 'fin_income' AND status = 'ok'
      AND last_success_date >= current_date - 30
""")["n"].iloc[0]
print(f"目标 {len(codes)} 只 · 已完成 {done} 只 · 待采 {len(codes) - done} 只",
      flush=True)

try:
    stats = Ingestor(db).sync_financials(codes)
    print("结果:", stats, flush=True)
    print(db.query("""
        SELECT count(DISTINCT code) 已采股票, count(*) 利润表行数,
               sum(CASE WHEN disclosure_date IS NOT NULL THEN 1 ELSE 0 END) 有披露日
        FROM fin_income
    """).to_string(index=False), flush=True)
finally:
    db.close()
PY

echo "[$(date +%H:%M:%S)] === 完成 ==="

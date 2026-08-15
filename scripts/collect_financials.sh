#!/bin/bash
# 采集财务数据（三大报表 + PIT 披露日期）
#
# 只能走东财详细接口：baostock 拿不到商誉、在建工程、合同负债、
# 资本开支——而这些正是红旗规则与行业指标最需要的字段。
# 故本脚本有限流风险，建议先小批量试跑。
#
# 全程幂等：随时 Ctrl+C，重跑自动跳过已完成的股票。
set -u
cd /Users/wwww/Documents/GitHub/CSG
LIMIT="${1:-0}"   # 传入数字则只采前 N 只，用于试跑

stamp() { echo "[$(date +%H:%M:%S)] $*"; }
stamp "=== 财务数据采集 ${LIMIT:+（试跑 $LIMIT 只）} ==="

.venv/bin/python -u - "$LIMIT" 2>&1 <<'PY' | grep --line-buffered -vE "it/s|^\s*$"
import logging, sys, warnings
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
from csg.storage import open_db
from csg.pipeline import Ingestor

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
db = open_db("data/csg.duckdb")

# 优先采「已有行情数据」的股票——三类数据齐全才能立刻用于验证
codes = db.query("""
    SELECT DISTINCT q.code FROM daily_quote q ORDER BY q.code
""")["code"].tolist()
if limit:
    codes = codes[:limit]

print(f"目标 {len(codes)} 只 × 3 表，预计 {len(codes)*21/60:.0f} 分钟", flush=True)
try:
    stats = Ingestor(db).sync_financials(codes)
    print("结果:", stats, flush=True)
    print(db.query("""
        SELECT count(*) AS 利润表行数, count(DISTINCT code) AS 股票数,
               count(DISTINCT report_period) AS 报告期数,
               sum(CASE WHEN disclosure_date IS NOT NULL THEN 1 ELSE 0 END) AS 有披露日
        FROM fin_income""").to_string(index=False), flush=True)
finally:
    db.close()
PY
stamp "=== 完成 ==="

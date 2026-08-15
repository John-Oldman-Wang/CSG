#!/bin/bash
# 补采缺失的行情。
# 上一轮 341/382 因 baostock 会话失效静默返回空，且被错误标记为 ok 水位，
# 故按「daily_quote 表中实际有无数据」判定，而非依赖水位。
set -u
cd /Users/wwww/Documents/GitHub/CSG
echo "[$(date +%H:%M:%S)] === 补采缺失行情 ==="
.venv/bin/python -u - 2>&1 <<'PY' | grep --line-buffered -vE "it/s|^\s*$"
import logging, warnings, datetime as dt
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
from csg.storage import open_db
from csg.pipeline import Ingestor
from csg.sources import baostock_source as bss

db = open_db("data/csg.duckdb")
missing = db.query("""
    SELECT DISTINCT r.code FROM research_report r
    WHERE NOT EXISTS (SELECT 1 FROM daily_quote q WHERE q.code = r.code)
    ORDER BY r.code
""")["code"].tolist()
print(f"缺失 {len(missing)} 只，开始补采", flush=True)
try:
    stats = Ingestor(db).sync_daily_quotes(
        missing, start=dt.date(2016,1,1), source="baostock", force=True)
    print("结果:", stats, flush=True)
    print(db.query("SELECT count(*) 行数, count(DISTINCT code) 股票数 FROM daily_quote").to_string(index=False), flush=True)
finally:
    bss.logout(); db.close()
PY
echo "[$(date +%H:%M:%S)] === 完成 ==="

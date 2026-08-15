#!/bin/bash
# 用 baostock 采集行情。东财在大量请求后限流封禁，行情接口连续全部失败；
# baostock 非爬虫架构，有独立数据服务，不受影响。
#
# python -u + grep --line-buffered：确保进度实时可见。
# 无可观测性的长任务无法判断健康状况——这本身就是缺陷。
set -u
cd /Users/wwww/Documents/GitHub/CSG
echo "[$(date +%H:%M:%S)] === baostock 行情采集 ==="
.venv/bin/python -u - 2>&1 <<'PY' | grep --line-buffered -vE "it/s|^\s*$"
import logging, warnings, datetime as dt
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")
from csg.storage import open_db
from csg.pipeline import Ingestor
from csg.sources import baostock_source as bss

db = open_db("data/csg.duckdb")
codes = db.query("SELECT DISTINCT code FROM research_report ORDER BY code")["code"].tolist()
print(f"目标 {len(codes)} 只（有研报数据的股票）", flush=True)
try:
    stats = Ingestor(db).sync_daily_quotes(
        codes, start=dt.date(2016,1,1), source="baostock", force=True)
    print("结果:", stats, flush=True)
finally:
    bss.logout(); db.close()
PY
echo "[$(date +%H:%M:%S)] === 完成 ==="

#!/bin/bash
# 优先采集「已有研报数据」那批股票的行情。
#
# 研报验证的硬阻塞是行情缺失——没有价格就算不出「发布后涨了没有」。
# 已入库 358 只的研报，先补齐这批的行情即可开跑，
# 比补全剩余研报更快产出结论。

set -u
cd /Users/wwww/Documents/GitHub/CSG
stamp() { echo "[$(date +%H:%M:%S)] $*"; }

stamp "=== 采集已有研报股票的行情 ==="
.venv/bin/python - <<'PY' 2>&1 | grep -vE "it/s|^\s*$"
import logging, warnings
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")
import datetime as dt
from csg.storage import open_db
from csg.pipeline import Ingestor

db = open_db("data/csg.duckdb")
codes = db.query(
    "SELECT DISTINCT code FROM research_report ORDER BY code"
)["code"].tolist()
print(f"目标 {len(codes)} 只（有研报数据的股票）")
stats = Ingestor(db).sync_daily_quotes(codes, start=dt.date(2016, 1, 1))
print("行情采集结果:", stats)
db.close()
PY
stamp "=== 行情采集结束 ==="

.venv/bin/python -c "
import warnings; warnings.filterwarnings('ignore')
from csg.storage import Database
db = Database('data/csg.duckdb', read_only=True)
print(db.query('''
  SELECT count(*) 行数, count(DISTINCT code) 股票数,
         min(trade_date) 最早, max(trade_date) 最晚
  FROM daily_quote''').to_string(index=False))
db.close()"

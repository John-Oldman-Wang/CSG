#!/bin/bash
# 定向补采指定股票的行情 + 财务 + 研报。
# 用于把关注标的快速纳入系统，无需等待全池采集。
set -u
cd /Users/wwww/Documents/GitHub/CSG
export TQDM_DISABLE=1 PYTHONUNBUFFERED=1
stamp() { echo ""; echo "[$(date +%H:%M:%S)] $*"; }

CODES="$*"
stamp "定向补采: $CODES"

.venv/bin/python -u - $CODES <<'PY'
import logging, sys, warnings, datetime as dt
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
from csg.storage import open_db
from csg.pipeline import Ingestor
from csg.sources import baostock_source as bss

codes = sys.argv[1:]
db = open_db("data/csg.duckdb")
ing = Ingestor(db)
try:
    print(f"[1/4] 行情 {codes}", flush=True)
    print("  ", ing.sync_daily_quotes(codes, start=dt.date(2016,1,1), source="baostock"), flush=True)
    print("[2/4] 估值", flush=True)
    print("  ", ing.sync_valuations(codes), flush=True)
    print("[3/4] 财务（东财详细接口，含披露日）", flush=True)
    print("  ", ing.sync_financials(codes), flush=True)
    print("[4/4] 研报", flush=True)
    print("  ", ing.sync_research(codes), flush=True)
finally:
    bss.logout(); db.close()
PY
stamp "完成"

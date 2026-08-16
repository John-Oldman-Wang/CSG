"""同花顺 F10 数据源 —— 补齐东财缺失的头部券商研报。

## 为什么需要这个源

东财免费研报接口（`ak.stock_research_report_em`）**不含任何头部券商**：
中信 / 中金 / 华泰 / 国泰君安 / 招商 / 广发 / 海通 / 兴业 / 申万 / 东方 全缺。
已核实为源头缺失而非采集遗漏（宁德时代接口返回 469 条、库中 469 条）。
推断：头部券商研究是卖给付费机构客户的产品，不授权免费渠道转发。

同花顺 F10 覆盖更全。实测宁德时代 300750：

    条数     东财 469  →  同花顺 607   （+29%）
    机构数   东财  39  →  同花顺  47
    跨度     2018-07-19 ~ 2026-08-06
    新增头部 华泰证券、东方证券、中国银河（news.html）
             招商证券、广发证券（worth.html 盈利预测明细）

仍拿不到：中信、中金、国泰君安、海通、中信建投、兴业、申万。
这七家在东财 / 慧博 / 同花顺三个免费渠道均无——是商业策略，不是技术问题。

## 两个页面

- `/{code}/news.html` —— 研报列表全量。数据在隐藏 div `#report_list_contents`
  里，是完整 JSON 数组，**一次请求拿全，无需翻页、无需执行 JS、无需登录**。
- `/{code}/worth.html` —— 盈利预测明细，含研究员与报告日期。
  这里有 news.html 没有的招商、广发。

## 合规

- `basic.10jqka.com.cn/robots.txt` 仅 `Disallow: /admin/`，并主动提供
  sitemap —— 内容页明确允许抓取。
- 只取**元数据**（机构 / 日期 / 评级 / 研究员 / 标题）。这些是事实性信息，
  不受著作权保护。**不下载 PDF 全文。**
- 请求间隔比东财更保守（3 秒），正常 UA + Referer，不伪装、不绕任何技术措施。
  若将来加了登录墙或验证码，到此为止。

## 已知偏差

同花顺评级分布比东财更极端：实测 300750 为 **买入 504 / 增持 103，
中性及以下 0 条**（东财尚有 0.66%）。做「按看空信号离场」类分析时，
该规则在此数据源上会**完全空转**，不是规则写错。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

import pandas as pd

from csg.sources.base import RateLimiter, call

log = logging.getLogger(__name__)

BASE = "http://basic.10jqka.com.cn"

# 比东财慢一倍：这是无 API 契约的页面抓取，宁可慢也不要造成访问压力
_limiter = RateLimiter(3.0, cooldown_after=5, cooldown_secs=120.0)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 研报列表藏在隐藏 div 里，整段是一个 JSON 数组
_REPORT_RE = re.compile(r'id="report_list_contents"[^>]*>(\[.*?\])</div>', re.DOTALL)

# 页面编码为 GBK —— requests 的自动探测在混合内容上会猜错，必须写死
_ENCODING = "gbk"


def _get(url: str, referer: str = "") -> str:
    """取回页面并按 GBK 解码。

    ⚠️ 不用 `resp.text`：requests 会依 charset 猜测，而该站部分页面
    未声明或声明不准，猜错时中文全变乱码——**且不报错**，
    只表现为机构名匹配不上、入库一堆问号。
    """
    import requests

    def _fetch() -> str:
        headers = {"User-Agent": _UA}
        if referer:
            headers["Referer"] = referer
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.content.decode(_ENCODING, errors="replace")

    _limiter.wait()
    try:
        html = call(_fetch, retries=3)
    except Exception:
        _limiter.record_failure()
        raise
    _limiter.record_success()
    return html


def fetch_reports(code: str) -> pd.DataFrame:
    """研报列表全量。

    返回列与东财适配器对齐，另加 `researcher` 与 `source`。
    空结果返回空 DataFrame（该股无覆盖，不是错误）。
    """
    url = f"{BASE}/{code}/news.html"
    html = _get(url, referer=f"{BASE}/{code}/")

    m = _REPORT_RE.search(html)
    if not m:
        # 页面结构变化必须显式暴露。静默返回空会被统计成 skipped，
        # 而本项目已三次栽在「失败被记成跳过」上（CLAUDE.md 第 2 条）。
        if "report_list_contents" in html:
            raise RuntimeError(f"{code} 找到 report_list_contents 但正则未匹配，页面结构可能已变")
        log.debug("%s 无研报区块", code)
        return pd.DataFrame()

    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{code} 研报 JSON 解析失败: {exc}") from exc

    if not items:
        return pd.DataFrame()

    rows = []
    for it in items:
        d = (it.get("date") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            continue
        inst = (it.get("source") or "").strip()
        title = (it.get("title") or "").strip()
        if not inst or not title:
            continue
        # 标题形如「国海证券：公司动态研究：…」，剥掉机构前缀避免与东财重复存储
        if title.startswith(f"{inst}："):
            title = title[len(inst) + 1:]
        rows.append({
            "code": code,
            "publish_date": dt.date.fromisoformat(d),
            "institution": inst,
            "title": title,
            "rating": (it.get("thspj") or "").strip() or None,
            "industry": None,
            "pdf_url": (it.get("url") or "").strip() or None,
            "researcher": (it.get("researcher") or "").strip() or None,
            "source": "ths",
            "snapshot_date": dt.date.today(),
        })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # 同一机构同日可能重复推送同一标题，主键去重前先自去重
    return df.drop_duplicates(["code", "publish_date", "institution", "title"])


# 盈利预测明细表：机构 | 研究员 | 各年 EPS | 各年净利 | 报告日期
_WORTH_RE = re.compile(
    r'<table[^>]*>(?=(?:(?!</table>).)*?机构名称)((?:(?!</table>).)*)</table>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def fetch_forecasts(code: str) -> pd.DataFrame:
    """盈利预测明细（长表：一行一个「机构 × 预测年份」）。

    **这个页面有 news.html 没有的招商证券、广发证券。** 两个页面都要取。

    表头形如：
        机构名称 | 研究员 | 预测年报每股收益（2026/2027/2028）
                        | 预测年报净利润（2026/2027/2028）| 报告日期
    年份是动态的（随时间推移滚动），故从表头解析而非写死。
    """
    url = f"{BASE}/{code}/worth.html"
    html = _get(url, referer=f"{BASE}/{code}/")

    m = _WORTH_RE.search(html)
    if not m:
        log.debug("%s 无盈利预测明细表", code)
        return pd.DataFrame()

    table = m.group(1)
    # 表头里的预测年份，按出现顺序即为各列对应年份
    head_end = table.find("</thead>")
    head = table[:head_end] if head_end > 0 else table[:2000]
    years = [int(y) for y in re.findall(r"\b(20\d\d)预测\b", _TAG_RE.sub(" ", head))]
    if not years:
        return pd.DataFrame()
    n_year = len(years) // 2 if len(years) >= 2 else len(years)
    years = years[:n_year]

    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.DOTALL):
        cells = [_TAG_RE.sub("", c).replace("&nbsp;", " ").strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.DOTALL)]
        cells = [c for c in cells if c != ""]
        if len(cells) < 3 + n_year:
            continue
        inst = cells[0]
        if not inst.endswith(("证券", "公司", "国际", "银河", "研究所")):
            continue
        rep_date = next((c for c in reversed(cells)
                         if re.fullmatch(r"\d{4}-\d{2}-\d{2}", c)), None)
        if not rep_date:
            continue

        eps_vals = []
        for c in cells[2:2 + n_year]:
            mm = _NUM_RE.search(c)
            eps_vals.append(float(mm.group()) if mm else None)

        for yr, eps in zip(years, eps_vals, strict=False):
            if eps is None:
                continue
            rows.append({
                "code": code,
                "publish_date": dt.date.fromisoformat(rep_date),
                "institution": inst,
                "forecast_year": yr,
                "eps": eps,
                # 本页不直接给 PE；隐含 PE 可由收盘价 ÷ eps 在分析层算，
                # 不在采集层做计算——采集只搬运事实
                "pe": None,
                "researcher": cells[1] or None,
                "source": "ths",
                "snapshot_date": dt.date.today(),
            })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(
        ["code", "publish_date", "institution", "forecast_year"])


def fetch_research(code: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """一次取回研报列表与盈利预测，签名与东财适配器一致。

    预测页失败不影响研报页：两者互相独立，一个挂掉不该让另一个白跑。
    """
    reports = fetch_reports(code)
    try:
        forecasts = fetch_forecasts(code)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s 盈利预测页失败（研报已取到 %d 条）: %s",
                    code, len(reports), exc)
        forecasts = pd.DataFrame()
    return reports, forecasts

#!/usr/bin/env python3
"""Run the existing DSA screening service and publish a checked watchlist."""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def text(value, limit=400):
    """Keep provider/LLM prose as bounded text, not embedded HTML or links."""
    result = html.escape(str(value or "未提供")[:limit]).replace("\n", " ")
    return re.sub(r"([\\`*\[\]_|])", r"\\\1", result)


def validate_history(frame, candidate, expected_date):
    """Fail closed on stale dates, suspension, broken OHLC or price mismatch."""
    import pandas as pd

    required = {"date", "close", "high", "low", "volume"}
    if frame is None or not required.issubset(frame.columns):
        raise ValueError("缺少可验证的日K字段")
    if frame.attrs.get("daily_stale"):
        raise ValueError("日K来自过期缓存")
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"].astype(str), errors="coerce")
    if data["date"].isna().any() or data["date"].duplicated().any():
        raise ValueError("日K日期异常")
    data = data.sort_values("date")
    if len(data) < 60 or data.iloc[-1]["date"].date() != expected_date:
        raise ValueError("不足60根日K或最后交易日不符")
    for column in required - {"date"}:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    recent = data.tail(60)
    values = recent[["close", "high", "low", "volume"]]
    if not values.apply(lambda column: column.map(lambda value: number(value) is not None)).all().all():
        raise ValueError("日K包含无效数值")
    if (values <= 0).any().any():
        raise ValueError("近期日K包含零成交量或无效价格")
    if ((recent["low"] > recent["close"]) | (recent["high"] < recent["close"])).any():
        raise ValueError("日K高低价关系异常")
    last = recent.iloc[-1]
    price = number(candidate.get("price"))
    if price is None or price <= 0 or abs(price / last["close"] - 1) > 0.005:
        raise ValueError("快照价格与目标日期日K不一致")
    return {
        "data_date": expected_date.isoformat(),
        "close": round(float(last["close"]), 3),
        "ma20": round(float(recent["close"].tail(20).mean()), 3),
        "prior_high20": round(float(recent["high"].iloc[-21:-1].max()), 3),
        "low5": round(float(recent["low"].tail(5).min()), 3),
    }


def assess(payload, expected_date, fetch_history):
    """Do not present data/LLM failures as a clean no-opportunity result."""
    issues = []
    picks = []
    candidates = payload.get("candidates") or []
    count = number(payload.get("snapshot_count"))
    if count is None or count < 1000:
        issues.append("全市场快照覆盖不足，暂停输出候选")
    notes = " ".join(map(str, payload.get("degradation") or []))
    if "last_good_cache" in notes or "stale_cache" in notes:
        issues.append("筛选过程使用了过期数据，暂停输出候选")
    if candidates and not payload.get("llm_ranked"):
        issues.append("AI排序未成功，不能标记为AI确认名单")
    if not candidates and ("enrichment row errors:" in notes or "fetch_failed" in notes):
        issues.append("日K抓取存在失败，不能将空结果解释为无机会")
    if not candidates and number(payload.get("after_filter_count")) not in (None, 0):
        if not payload.get("daily_enrich_count"):
            issues.append("日K验证未成功，不能判断市场是否存在候选")
    if issues:
        return {"status": "unavailable", "picks": [], "issues": issues}

    excluded = []
    seen = set()
    for candidate in candidates[:5]:
        code = str(candidate.get("code", ""))
        raw = candidate.get("raw") or {}
        name = str(candidate.get("name") or "")
        if not re.fullmatch(r"(?:00|30|60|68)\d{4}", code):
            excluded.append(f"{code}: 本版仅覆盖沪深A股")
            continue
        if code in seen:
            continue
        seen.add(code)
        if not name or re.search(r"ST|退|^[NC]", name, re.I):
            excluded.append(f"{code}: 风险警示、退市或新股标识")
            continue
        if candidate.get("risk_level") == "high" or raw.get("excluded_by_risk"):
            excluded.append(f"{code}: 风险层否决")
            continue
        confidence = number(candidate.get("llm_confidence"))
        if confidence is None or confidence < 0.5:
            excluded.append(f"{code}: AI置信度不足或缺失（非胜率）")
            continue
        try:
            frame, source = fetch_history(code, lookback_days=120)
            verified = validate_history(frame, candidate, expected_date)
        except Exception as exc:
            # Only controlled validation messages go into user-visible output.
            issues.append(f"{code}: 日K日期、价格或完整性核验失败（{type(exc).__name__}）")
            continue
        picks.append({**candidate, "verified": verified, "history_source": source})
    status = "partial" if issues else ("ready" if picks else "empty")
    return {"status": status, "picks": picks, "issues": issues, "excluded": excluded}


def render_report(payload, assessment, as_of):
    status = assessment["status"]
    labels = {"ready": "待确认候选", "partial": "部分数据未通过核验", "empty": "无合适候选", "unavailable": "本次筛选不可用"}
    lines = [f"# A股短线选股｜{as_of.isoformat()}", "", f"**状态：{labels[status]}**", "",
             "观察周期：约2～10个交易日；这是收盘后观察名单，不是立即买入指令。",
             f"快照覆盖：{text(payload.get('snapshot_count'))}只；来源：{text(payload.get('snapshot_source'))}。",
             "先按快照过滤，再对前30名补充日K；不代表对全市场逐只完成历史分析。", ""]
    if status == "empty":
        lines.append("本次策略未留下合适候选，不为凑数量放宽条件。")
    if status == "unavailable":
        lines.append("没有输出候选，不等于市场没有机会；请检查运行日志。")
    for index, pick in enumerate(assessment["picks"], 1):
        v = pick["verified"]
        lines.extend([f"## {index}. {text(pick['name'])}（{pick['code']}）", "",
                      f"已核验日K日期：{v['data_date']}；收盘价：{v['close']}元。",
                      f"入选理由（AI观点）：{text(pick.get('llm_thesis') or pick.get('reason'))}",
                      f"次日确认条件（AI观点）：{text('；'.join(pick.get('llm_watch_items') or []))}",
                      f"失效条件（AI观点）：{text('；'.join(pick.get('llm_invalidators') or []))}",
                      f"风险：{text('；'.join((pick.get('llm_risks') or []) + (pick.get('risk_flags') or [])))}",
                      f"参考位置：MA20 {v['ma20']}；此前20日最高价 {v['prior_high20']}；近5日最低价 {v['low5']}。",
                      "上述位置是历史指标，不是自动买入价或保证可成交的止损价。", ""])
        news = pick.get("dsa_news") or []
        lines.append("资讯核验：" + ("已有资讯，仍需核对公司公告。" if news else "未取得新闻/公告证据，相关事件与财务风险尚未完整核验。"))
    for issue in assessment.get("issues", []):
        lines.append(f"- 数据问题：{text(issue)}")
    for issue in assessment.get("excluded", []):
        lines.append(f"- 已剔除：{text(issue)}")
    warnings = (payload.get("warnings") or []) + (payload.get("degradation") or [])
    if warnings:
        lines.extend(["", "数据覆盖与降级记录："])
        lines.extend(f"- {text(warning, 220)}" for warning in warnings[:10])
    lines.extend(["", "模型置信度与排序分数不是上涨概率。策略未经收益回测验证；A股T+1，次日跳空和跌停可能使止损无法按预期执行。"])
    return "\n\n".join(lines)


def execute(*, screen, fetch_history, send, expected_date, output_dir, dry_run=False):
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = screen(strategy="short_term_watch", market="cn", max_results=5)
    except Exception:
        import logging
        logging.exception("短线筛选引擎执行失败")
        payload = {}
        assessment = {"status": "unavailable", "picks": [], "issues": ["筛选引擎执行失败，请检查Actions日志"]}
    else:
        assessment = assess(payload, expected_date, fetch_history)
    report = render_report(payload, assessment, expected_date)
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    (output_dir / "result.json").write_text(json.dumps(
        {"as_of": expected_date.isoformat(), "assessment": assessment, "screening": payload},
        ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")
    if not dry_run and not send(report):
        raise RuntimeError("推送请求未成功，请检查PushPlus日志；报告已保存")
    return 1 if assessment["status"] in {"unavailable", "partial"} else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-run", action="store_true", help="非交易日手动检查最近收盘；仍校验数据日期")
    parser.add_argument("--dry-run", action="store_true", help="真实取数和AI分析，保存报告但不推送")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/short_term"))
    args = parser.parse_args(argv)
    from src.config import get_config
    from src.core.trading_calendar import build_market_phase_context, MarketPhase
    from src.notification import NotificationService
    from src.services.screening_service import ScreeningService, get_dsa_daily_history

    ctx = build_market_phase_context(market="cn")
    if ctx.phase == MarketPhase.NON_TRADING and not args.force_run:
        print("非交易日，跳过短线选股")
        return 0
    if ctx.phase not in {MarketPhase.POSTMARKET, MarketPhase.NON_TRADING}:
        raise RuntimeError("本任务仅在收盘后运行；交易日历未知或盘中时停止")
    if ctx.effective_daily_bar_date is None:
        raise RuntimeError("无法确认最近完整交易日")
    as_of = ctx.effective_daily_bar_date
    if not isinstance(as_of, date):
        as_of = date.fromisoformat(str(as_of))
    config = get_config()
    service = ScreeningService(config)
    notifier = NotificationService()
    result = execute(screen=service.screen, fetch_history=get_dsa_daily_history,
                     send=lambda report: notifier.send(report, route_type="report"),
                     expected_date=as_of, output_dir=args.output_dir, dry_run=args.dry_run)
    print(f"短线选股完成：{as_of}，退出码{result}；PushPlus接收请求不等于微信已送达")
    return result


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())

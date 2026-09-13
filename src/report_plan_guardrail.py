# -*- coding: utf-8 -*-
"""Deterministic consistency checks for Chinese trading-plan reports.

These checks validate model-proposed levels; they do not validate their market
merit or turn them into an executable order.
"""
from __future__ import annotations

import math
import re


def _single_price(value):
    """Reject ranges, multiple levels and indicator/percentage-only strings."""
    if isinstance(value, bool):
        return None
    text = str(value or '').strip()
    if ',' in text or re.search(r'-\s*\d+(?:\.\d+)?\s*元', text):
        return None
    if re.search(r'\d\s*[-~～至]\s*\d', text):
        return None
    amounts = re.findall(r'(?<![\d.])([0-9]+(?:\.[0-9]+)?)\s*元', text)
    if not amounts and re.fullmatch(r'\d+(?:\.\d+)?', text):
        amounts = [text]
    unique = set(amounts)
    if len(unique) != 1:
        return None
    number = float(unique.pop())
    return number if math.isfinite(number) and number > 0 else None


def enforce_report_plan(result, phase_summary, overview, language):
    """Normalize plans after decision downgrades, before persistence/rendering."""
    if language != 'zh' or not isinstance(result.dashboard, dict):
        return
    dashboard = result.dashboard
    battle = dashboard.get('battle_plan')
    if not isinstance(battle, dict):
        return
    blocks = {
        b.get('key'): b.get('status')
        for b in (overview or {}).get('blocks', []) if isinstance(b, dict)
    }
    intel = dashboard.get('intelligence')
    if isinstance(intel, dict) and blocks.get('news') != 'available':
        intel['sentiment_summary'] = '新闻证据未完整核实，舆情未知；不能据此判定中性或利好。'
    # Model-generated percentages have no calibrated attribution contract.
    attr = dashboard.get('signal_attribution')
    if isinstance(attr, dict):
        for key in ('technical_indicators', 'news_sentiment', 'fundamentals', 'market_conditions'):
            attr.pop(key, None)
    phase = (phase_summary or {}).get('phase', 'unknown')
    date = (phase_summary or {}).get('effective_daily_bar_date') or '未知'
    core = dashboard.get('core_conclusion')
    if isinstance(core, dict):
        core['time_sensitivity'] = f'日线截至 {date}；行情或条件变化后需重新评估'
    pd = dashboard.get('phase_decision', {})
    pd['next_check_time'] = '需重新获取行情后核对；本报告不新增自动检查任务'
    sniper = battle.get('sniper_points')
    if not isinstance(sniper, dict):
        sniper = {}
    keys = ('ideal_buy', 'secondary_buy', 'stop_loss', 'take_profit')
    prices = {k: _single_price(sniper.get(k)) for k in keys}
    entry, stop, target = (prices[k] for k in ('ideal_buy', 'stop_loss', 'take_profit'))
    secondary = prices['secondary_buy']
    valid = all(v is not None for v in (entry, stop, target))
    valid = valid and stop < entry < target
    if sniper.get('secondary_buy') not in (None, '', 'N/A', '-'):
        valid = valid and secondary is not None and stop < secondary < target
    # A reference level is never proof that intraday confirmation has occurred.
    blocked = (
        phase != 'intraday' or getattr(result, 'decision_type', 'hold') != 'buy'
        or any(blocks.get(k) != 'available' for k in ('quote', 'daily_bars', 'technical'))
        or any(w in str(pd.get('immediate_action', '')) for w in ('等待', '观察', '禁止'))
    )
    status = '未触发：仅为条件预案，不新增仓位。' if blocked else '候选预案：仍需核对实时触发条件，并非自动下单。'
    if blocked or not valid:
        reason = ('交易价位未通过一致性校验，等待重新评估。' if not valid
                  else '入场条件尚未核实，当前仅观察，不据此新增仓位。')
        # An explicitly unresolved action is a fail-closed contract: do not
        # manufacture a resolved watch signal for persistence/downstream APIs.
        unresolved = hasattr(result, 'action') and result.action is None
        if getattr(result, 'decision_type', 'hold') == 'buy' and not unresolved:
            result.decision_type = 'hold'
            result.operation_advice = '观望'
            result.action = 'watch'
            result.guardrail_reason = reason
            result.analysis_summary = reason
            if isinstance(core, dict):
                core['one_sentence'] = reason
                core['signal_type'] = '🟡持有观望'
        if isinstance(core, dict):
            position_advice = core.setdefault('position_advice', {})
            if isinstance(position_advice, dict):
                position_advice['no_position'] = reason
        if getattr(result, 'decision_type', 'hold') != 'sell':
            pd['immediate_action'] = reason
            if isinstance(core, dict):
                core['one_sentence'] = reason
                advice = core.get('position_advice')
                if isinstance(advice, dict):
                    advice['has_position'] = '不根据本预案加仓；核对已有仓位、可卖数量和风险预算后重新评估。'
    risk = ('未提供账户资金、已有持仓及可卖数量，不给固定仓位或账户风险百分比。'
            '参考退出价不保证成交；A股新买股份不能当日卖出，跳空、跌停可能扩大实际亏损。')
    if not valid:
        message = '价位校验未通过：多重价位、缺失或止损/入场/目标顺序冲突；原交易预案撤回，等待重新评估。'
        battle['sniper_points'] = {k: '待重新评估（不作为交易指令）' for k in keys}
        battle['position_strategy'] = {
            'suggested_position': '不据此新增仓位',
            'entry_plan': message,
            'risk_control': risk,
        }
        battle['action_checklist'] = [message]
    else:
        battle['sniper_points'] = {
            k: (f'{v:.2f} 元（模型参考位，条件尚需核实）' if v is not None else '未提供')
            for k, v in prices.items()
        }
        calculations = []
        for label, buy in [('首选', entry), ('备选', secondary)]:
            if buy is not None:
                calculations.append(
                    f'{label}入场 {buy:.2f} 元：到退出位风险 {(buy-stop)/buy*100:.2f}%，'
                    f'到目标收益 {(target-buy)/buy*100:.2f}%，收益风险比 {(target-buy)/(buy-stop):.2f}'
                )
        battle['position_strategy'] = {
            'suggested_position': '未计算（缺少账户风险预算与持仓）',
            'entry_plan': status + '首选与备选为独立情景，不是跌破止损后继续补仓。',
            'risk_control': '；'.join(calculations) + '。未计费用、滑点和跳空，不代表胜率。' + risk,
        }
        battle['action_checklist'] = [status, '价格顺序校验通过；尚未验证策略收益或实际成交条件。']

"""Regression scenarios from inconsistent live trading plans."""
import unittest
from types import SimpleNamespace

from src.phase_decision_guardrail import apply_phase_decision_guardrails
from src.report_plan_guardrail import _single_price


class ReportPlanTests(unittest.TestCase):
    def result(self, entry='17.36 元', stop='16.90 元', secondary='17.17 元'):
        return SimpleNamespace(
            confidence_level='中', report_language='zh', decision_type='hold',
            operation_advice='持有', analysis_summary='',
            dashboard={
                'core_conclusion': {},
                'intelligence': {'sentiment_summary': '中性偏多'},
                'signal_attribution': {'news_sentiment': 7, 'technical_indicators': 93},
                'battle_plan': {
                    'sniper_points': dict(ideal_buy=entry, secondary_buy=secondary,
                                         stop_loss=stop, take_profit='18.50 元'),
                    'position_strategy': {'entry_plan': '买两成，跌到止损下面补仓'},
                },
            },
        )

    def apply(self, r):
        apply_phase_decision_guardrails(
            r, market_phase_summary={'phase': 'non_trading', 'market': 'cn',
                                     'effective_daily_bar_date': '2026-09-11'},
            analysis_context_pack_overview={'blocks': [{'key': 'news', 'status': 'missing'}]},
        )
        return r.dashboard['battle_plan']

    def test_wait_state_replaces_position_instructions_and_calculates_entry_risk(self):
        r = self.result()
        b = self.apply(r)
        self.assertIn('未触发', b['position_strategy']['entry_plan'])
        self.assertNotIn('买两成', str(b))
        self.assertIn('2.48', b['position_strategy']['risk_control'])
        self.assertIn('2.65%', b['position_strategy']['risk_control'])
        self.assertIn('舆情未知', r.dashboard['intelligence']['sentiment_summary'])
        self.assertNotIn('news_sentiment', r.dashboard['signal_attribution'])

    def test_secondary_below_stop_withdraws_entire_plan(self):
        b = self.apply(self.result(entry='30.13 元', stop='29.00 元', secondary='27.29 元'))
        self.assertIn('原交易预案撤回', b['position_strategy']['entry_plan'])
        self.assertNotIn('27.29', str(b))

    def test_ambiguous_stop_and_target_are_not_silently_parsed(self):
        for value in ['29.00元；跌破27.29元清仓', '29-30元', 'MA20', 'NaN', '-3元', '1,030元', True]:
            self.assertIsNone(_single_price(value))
        b = self.apply(self.result(stop='16.90元 或16.50元'))
        self.assertIn('撤回', b['position_strategy']['entry_plan'])

    def test_reapplication_keeps_withdrawn_plan_withdrawn(self):
        r = self.result(stop='16.90元 或16.50元')
        self.apply(r)
        self.apply(r)
        self.assertIn('撤回', r.dashboard['battle_plan']['position_strategy']['entry_plan'])

    def test_invalid_buy_is_downgraded_in_summary_and_action(self):
        r = self.result(stop='29元', secondary='27.29元')
        r.decision_type = 'buy'
        self.apply(r)
        self.assertEqual(r.decision_type, 'hold')
        self.assertEqual(r.action, 'watch')
        self.assertIn('一致性校验', r.dashboard['core_conclusion']['one_sentence'])
        self.assertIn('一致性校验', r.dashboard['core_conclusion']['position_advice']['no_position'])

    def test_indicator_numbers_do_not_become_prices(self):
        self.assertEqual(_single_price('17.36元（MA5，-5%）'), 17.36)


if __name__ == '__main__':
    unittest.main()

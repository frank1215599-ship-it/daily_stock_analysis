"""Regression cases for screened-but-unverified daily watchlists."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from tests.test_short_term_screening_task import task, history, payload


def strong_history():
    frame = history()
    frame['close'] = [10 + i * .02 for i in range(len(frame))]
    frame['high'] = frame.close + .08
    frame['low'] = frame.close - .08
    frame['open'] = frame.close - .01
    frame.loc[64, 'volume'] = 180000
    return frame


class ScreeningReliabilityTests(unittest.TestCase):
    def test_stale_primary_recovers_on_independently_validated_source(self):
        frame = strong_history()
        candidate = {**payload()['candidates'][0], 'price': float(frame.close.iloc[-1])}
        attempts = []
        def fallback(code, **kwargs):
            attempts.append(kwargs['source'])
            return frame, kwargs['source']
        result = task.assess({**payload(), 'candidates': [candidate]}, frame.date.iloc[-1].date(),
                             lambda *a, **k: (frame.iloc[:-1], 'test_primary'), fallback)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(attempts, ['tencent'])
        self.assertIn('日K末日', result['verification'][candidate['code']][0]['reason'])
        self.assertEqual(result['picks'][0]['history_source'], 'tencent')

    def test_fallback_does_not_relax_daily_filters(self):
        frame = history()
        result = task.assess(payload(), frame.date.iloc[-1].date(),
                             lambda *a, **k: (frame.iloc[:-1], 'test'),
                             lambda *a, **k: (frame, k['source']))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['picks'], [])
        self.assertIn('未通过原技术筛选', result['issues'][0])

    def test_errors_are_actionable_but_do_not_leak_provider_payload(self):
        def failed(*a, **k):
            raise RuntimeError('secret-token-in-provider-url')
        result = task.assess(payload(), history().date.iloc[-1].date(), failed, failed)
        self.assertEqual(len(result['verification']['600001']), 3)
        self.assertNotIn('secret-token', str(result))
        bad = history()
        with self.assertRaisesRegex(task.HistoryValidationError, '快照价9.0.*0.5%'):
            task.validate_history(bad, {'price':9}, bad.date.iloc[-1].date())

    def test_batches_continue_stop_and_respect_cap(self):
        from src.services.screening.pipeline import _enrich_short_term_batches
        from src.services.screening.models import HardFilterConfig
        screening = SimpleNamespace(hard_filters=HardFilterConfig(exclude_st=False, signal_score_min=60))
        rows = pd.DataFrame({'code':[str(i) for i in range(192)], 'signal_score':[0]*30 + [70]*162})
        calls = []
        def enrich(frame, **kwargs):
            calls.append(list(frame.code))
            frame = frame.copy()
            frame.attrs = {'daily_success_count':len(frame), 'daily_source_counts':{'fixture':len(frame)}}
            return frame
        with patch('src.services.screening.pipeline.enrich_daily_features', side_effect=enrich):
            output, count = _enrich_short_term_batches(rows, screening=screening, target=5, limit=90)
            self.assertEqual(count,60)
            self.assertEqual(output.attrs['daily_success_count'],60)
            self.assertEqual(len(set(sum(calls, []))),60)
            rows.signal_score = 0
            output, count = _enrich_short_term_batches(rows, screening=screening, target=5, limit=150)
            self.assertEqual(count,90)

    def test_report_deduplicates_warnings(self):
        report = task.render_report({**payload(), 'warnings':['same warning'], 'degradation':['same warning']},
                                    {'status':'empty','picks':[]}, history().date.iloc[-1].date())
        self.assertEqual(report.count('same warning'),1)
        self.assertIn('最多90只',report)

    def test_pushplus_uses_distinct_title(self):
        from src.config import Config
        from src.notification_sender.pushplus_sender import PushplusSender
        sender = PushplusSender(Config(stock_list=[], pushplus_token='test'))
        with patch.object(sender, '_send_pushplus_message', return_value=True) as send:
            self.assertTrue(sender.send_to_pushplus('# A股短线选股｜2026-09-16\n\n状态'))
            self.assertEqual(send.call_args.args[-1], 'A股短线选股｜2026-09-16')

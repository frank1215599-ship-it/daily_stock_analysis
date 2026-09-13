"""Independent scalar reference and fail-closed tests for CC FGI."""
import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

from src.fear_greed import calculate_fear_greed, attach_fear_greed, render_fear_greed


def fixture():
    rng = np.random.default_rng(17)
    close = 30 + np.cumsum(rng.normal(0, 0.8, 100))
    return pd.DataFrame({'date': pd.date_range('2025-01-01', periods=100),
                         'close': close, 'high': close + 0.7, 'low': close - 0.5})


def scalar_reference(df):
    """Direct chronological loop using all bars, no pandas rolling/tail path."""
    rows = list(df.itertuples())
    signed = [0.0]
    for previous, current in zip(rows, rows[1:]):
        tr = max(current.high-current.low, abs(current.high-previous.close),
                 abs(current.low-previous.close))
        signed.append(tr if current.close > previous.close else -tr if current.close < previous.close else 0)
    def weighted(values):
        n = len(values)
        return sum((i+1)*value for i, value in enumerate(values)) / (n*(n+1)/2)
    differences = [weighted(signed[i-9:i+1])-weighted(signed[i-29:i+1]) for i in range(29, len(signed))]
    return [(a+2*b)/3 for a,b in zip(differences, differences[1:])]


class FearGreedTest(unittest.TestCase):
    def test_matches_independent_reference_with_lag_and_crosses(self):
        df = fixture()
        seen = set()
        for end in range(35, 101):
            bars = df.iloc[:end]
            result = calculate_fear_greed(bars)
            expected = scalar_reference(bars.iloc[:-1])
            self.assertEqual(result['status'], 'available')
            self.assertAlmostEqual(result['value'], expected[-1], places=12)
            self.assertAlmostEqual(result['previous_value'], expected[-2], places=12)
            event = 'bullish_cross' if expected[-1] > 0 and expected[-2] <= 0 else 'bearish_cross' if expected[-1] < 0 and expected[-2] >= 0 else 'none'
            self.assertEqual(result['event'], event)
            seen.add(event)
        self.assertEqual(seen, {'bullish_cross', 'bearish_cross', 'none'})

    def test_latest_live_bar_cannot_change_confirmed_signal(self):
        df = fixture()
        expected = calculate_fear_greed(df)
        df.loc[99, ['high','low','close']] = [1000, 0, 1000]
        self.assertEqual(expected, calculate_fear_greed(df))
        self.assertEqual(expected['source_bar_date'], '2025-04-09')
        self.assertEqual(expected['display_bar_date'], '2025-04-10')

    def test_flat_then_cross_from_zero(self):
        df = fixture().iloc[:40].copy()
        df[['high','low','close']] = [31,29,30]
        self.assertEqual(calculate_fear_greed(df)['state'], 'neutral')
        df.loc[38, ['high','low','close']] = [32,30,31]
        self.assertEqual(calculate_fear_greed(df)['event'], 'bullish_cross')
        df.loc[38, ['high','low','close']] = [30,28,29]
        self.assertEqual(calculate_fear_greed(df)['event'], 'bearish_cross')

    def test_invalid_data_never_becomes_neutral(self):
        for value in [np.nan, np.inf, -1]:
            df = fixture()
            df.loc[98, 'close'] = value
            result = calculate_fear_greed(df)
            self.assertEqual(result['status'], 'unavailable')
            self.assertNotIn('state', result)
        df = fixture()
        df.loc[98, 'date'] = df.loc[97, 'date']
        self.assertEqual(calculate_fear_greed(df)['status'], 'unavailable')
        self.assertEqual(calculate_fear_greed(fixture().iloc[:34])['status'], 'unavailable')
        self.assertEqual(calculate_fear_greed(fixture().drop(columns='high'))['status'], 'unavailable')

    def test_sorted_and_immutable(self):
        df = fixture()
        original = df.copy(deep=True)
        self.assertEqual(calculate_fear_greed(df), calculate_fear_greed(df.iloc[::-1]))
        pd.testing.assert_frame_equal(df, original)

    def test_trend_to_full_and_single_report(self):
        from unittest.mock import patch
        from src.stock_analyzer import StockTrendAnalyzer
        from src.analyzer import AnalysisResult
        from src.notification import NotificationService
        from src.config import Config

        bars = fixture().assign(volume=100000)
        trend = StockTrendAnalyzer().analyze(bars, '000001')
        self.assertEqual(trend.to_dict()['fear_greed'], calculate_fear_greed(bars))
        result = AnalysisResult(code='000001', name='测试股票', sentiment_score=50,
                                trend_prediction='震荡', operation_advice='观望', dashboard={})
        attach_fear_greed(result, trend)
        with patch('src.notification.get_config', return_value=Config(stock_list=[], report_language='zh')):
            service = NotificationService()
            service._report_summary_only = False
            full = service.generate_dashboard_report([result])
            single = service.generate_single_stock_report(result)
        for report in [full, single]:
            self.assertIn('FGI [CC]', report)
            self.assertIn('2025-04-09', report)
            self.assertIn('无新转向' if trend.fear_greed['event'] == 'none' else '穿零轴', report)

    def test_runtime_facts_replace_llm_values_without_changing_decision(self):
        result = SimpleNamespace(dashboard={'fear_greed': {'value': 999}}, decision_type='hold')
        facts = calculate_fear_greed(fixture())
        attach_fear_greed(result, SimpleNamespace(fear_greed=facts))
        self.assertEqual(result.dashboard['fear_greed'], facts)
        self.assertEqual(result.decision_type, 'hold')
        text = render_fear_greed(result.dashboard['fear_greed'])
        self.assertIn('2025-04-09', text)
        self.assertIn('不重绘', text)
        attach_fear_greed(result, None)
        self.assertEqual(result.dashboard['fear_greed']['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()

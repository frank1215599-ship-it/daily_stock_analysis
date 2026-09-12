"""Offline contracts for the scheduled watchlist; no network or paid calls."""
import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("short_term_task", ROOT / "scripts/run_short_term_screening.py")
task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task)


def history():
    return pd.DataFrame({"date": pd.bdate_range(end="2026-09-11", periods=65),
                         "close": 10.0, "high": 10.5, "low": 9.5, "volume": 100000})


def payload():
    return {"snapshot_count": 5000, "after_filter_count": 30, "daily_enrich_count": 30,
            "snapshot_source": "fixture", "llm_ranked": True, "candidates": [
                {"code": "600001", "name": "测试股票", "price": 10, "llm_confidence": 0.8,
                 "risk_level": "low", "raw": {}, "llm_thesis": "测试观点"}]}


class ShortTermTaskTests(unittest.TestCase):
    def setUp(self):
        self.as_of = date(2026, 9, 11)

    def test_reject_stale_future_duplicate_and_zero_volume_bars(self):
        cases = []
        cases.append(history().iloc[:-1])
        duplicate = history()
        duplicate.loc[64, "date"] = duplicate.loc[63, "date"]
        cases.append(duplicate)
        zero = history()
        zero.loc[64, "volume"] = 0
        cases.append(zero)
        future = history()
        future.loc[64, "date"] = pd.Timestamp("2026-09-14")
        cases.append(future)
        bad = history()
        bad.loc[64, "close"] = float("inf")
        cases.append(bad)
        for frame in cases:
            with self.subTest(frame=frame.tail(1).to_dict()), self.assertRaises(ValueError):
                task.validate_history(frame, {"price": 10}, self.as_of)

    def test_old_snapshot_price_rejected(self):
        with self.assertRaises(ValueError):
            task.validate_history(history(), {"price": 9}, self.as_of)

    def test_llm_failure_is_not_empty_market(self):
        data = payload()
        data["llm_ranked"] = False
        result = task.assess(data, self.as_of, lambda *a, **k: self.fail("must not fetch"))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["picks"], [])

    def test_low_snapshot_coverage_and_stale_fallback_block_output(self):
        for overrides in ({"snapshot_count": 30}, {"degradation": ["last_good_cache stale"]}):
            result = task.assess({**payload(), **overrides}, self.as_of, lambda *a, **k: (history(), "test"))
            self.assertEqual(result["status"], "unavailable")

    def test_valid_empty_screen_does_not_require_llm_call(self):
        data = {**payload(), "candidates": [], "after_filter_count": 0, "llm_ranked": False}
        self.assertEqual(task.assess(data, self.as_of, None)["status"], "empty")

    def test_daily_failures_are_not_hidden_by_zero_after_filter_count(self):
        data = {**payload(), "candidates": [], "after_filter_count": 0,
                "degradation": ["Daily K-line enrichment row errors: timeout"]}
        self.assertEqual(task.assess(data, self.as_of, None)["status"], "unavailable")

    def test_strategy_loads_and_real_filter_rejects_hot_illiquid_or_weak_rows(self):
        from src.services.screening.strategy import load_all_strategies
        from src.services.screening.filter import apply_hard_filters
        strategy = load_all_strategies(ROOT / "src/services/screening/strategies")["short_term_watch"]
        row = {"code": "600001", "name": "测试", "price": 10, "amount": 500000000,
               "total_mv": 5000000000, "pe_ratio": 20, "pb_ratio": 2, "turnover_rate": 4,
               "volume_ratio": 2, "change_pct": 3, "price_above_ma20": True,
               "signal_score": 70, "macd_status": "bullish", "breakout_20d_pct": 1,
               "volume_ratio_20d": 2, "range_20d_pct": 20, "atr_20_pct": 3}
        rows = [row, {**row, "code": "600002", "amount": 1000000},
                {**row, "code": "600003", "change_pct": 9.9},
                {**row, "code": "600004", "price_above_ma20": False},
                {**row, "code": "600005", "volume_ratio_20d": float("nan")}]
        filtered = apply_hard_filters(pd.DataFrame(rows), strategy.screening.hard_filters)
        self.assertEqual(filtered["code"].tolist(), ["600001"])

    def test_data_failure_is_partial_not_empty(self):
        result = task.assess(payload(), self.as_of, lambda *a, **k: (history().iloc[:-1], "test"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["picks"], [])

    def test_risk_flags_and_low_confidence_exclude_candidates(self):
        for override in ({"name": "*ST测试"}, {"risk_level": "high"}, {"llm_confidence": 0.4}, {"code": "920001"}):
            data = payload()
            data["candidates"][0].update(override)
            result = task.assess(data, self.as_of, lambda *a, **k: (history(), "test"))
            self.assertEqual(result["picks"], [])
            self.assertTrue(result["excluded"])

    def test_execution_writes_report_and_dry_run_does_not_send(self):
        with tempfile.TemporaryDirectory() as directory:
            code = task.execute(screen=lambda **k: payload(), fetch_history=lambda *a, **k: (history(), "test"),
                                send=lambda report: self.fail("dry run sent"), expected_date=self.as_of,
                                output_dir=Path(directory), dry_run=True)
            self.assertEqual(code, 0)
            report = (Path(directory) / "report.md").read_text()
            self.assertIn("600001", report)
            self.assertIn("2026-09-11", report)
            self.assertIn("未取得新闻", report)
            self.assertTrue((Path(directory) / "result.json").exists())

    def test_notification_failure_keeps_artifact_and_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                task.execute(screen=lambda **k: payload(), fetch_history=lambda *a, **k: (history(), "test"),
                             send=lambda report: False, expected_date=self.as_of, output_dir=Path(directory))
            self.assertTrue((Path(directory) / "report.md").exists())

    def test_report_escapes_untrusted_markup(self):
        data = payload()
        data["candidates"][0]["llm_thesis"] = '<script>alert(1)</script> [click](https://example.com)'
        assessment = task.assess(data, self.as_of, lambda *a, **k: (history(), "test"))
        report = task.render_report(data, assessment, self.as_of)
        self.assertNotIn("<script>", report)
        self.assertNotIn("[click]", report)

    def test_ranking_diagnostics_keep_response_text_private(self):
        from src.services.screening.ranker import _extract_completion_text
        response = {"choices": [{"finish_reason": "length", "message": {
            "content": "", "reasoning_content": "PRIVATE_REASONING"}}],
            "usage": {"completion_tokens": 2048}}
        with self.assertLogs("src.services.screening.ranker", level="INFO") as logs:
            self.assertEqual(_extract_completion_text(response), "")
        self.assertIn("finish_reason=length", " ".join(logs.output))
        self.assertIn("completion_tokens=2048", " ".join(logs.output))
        self.assertNotIn("PRIVATE_REASONING", " ".join(logs.output))

    def test_workflow_uses_existing_secrets_and_separate_schedule(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/short-term-screening.yml").read_text())
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(trigger["schedule"], [{"cron": "15 8 * * 1-5"}])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        env = workflow["jobs"]["screen"]["steps"][3]["env"]
        self.assertNotIn("STOCK_LIST", env)
        self.assertEqual(env["SCREENING_FALLBACK_SNAPSHOT_PATH"], "off")


if __name__ == "__main__":
    unittest.main()

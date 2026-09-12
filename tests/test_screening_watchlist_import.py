import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from import_screening_watchlist import candidate_codes, fetch_latest, merge_codes, read_archive


class WatchlistImportTests(unittest.TestCase):
    def payload(self):
        return {"as_of": "2026-09-11", "assessment": {"status": "ready", "picks": [
            {"code": "002988", "verified": {"data_date": "2026-09-11"}, "llm_confidence": .8},
            {"code": "600001", "verified": {"data_date": "2026-09-11"}, "llm_confidence": .7}]}}

    def test_verified_candidates_merge_without_replacing_base(self):
        codes = candidate_codes(self.payload(), "2026-09-11")
        self.assertEqual(merge_codes("002988,600391,002272,601698", codes),
                         ["002988", "600391", "002272", "601698", "600001"])

    def test_stale_and_failed_results_are_rejected(self):
        for status in ("empty", "partial", "unavailable"):
            p = self.payload()
            p["assessment"]["status"] = status
            with self.assertRaises(ValueError):
                candidate_codes(p, "2026-09-11")
        with self.assertRaises(ValueError):
            candidate_codes(self.payload(), "2026-09-14")

    def test_invalid_candidates_are_rejected(self):
        for field, value in (("code", "600001\nEVIL=1"), ("llm_confidence", float("nan")),
                             ("risk_level", "high"), ("verified", {"data_date": "2026-09-10"})):
            p = self.payload()
            p["assessment"]["picks"][0][field] = value
            with self.assertRaises(ValueError):
                candidate_codes(p, "2026-09-11")

    def test_latest_failed_run_blocks_previous_success(self):
        session = Mock()
        session.get.return_value.json.return_value = {"workflow_runs": [
            {"id": 2, "head_branch": "main", "status": "completed", "conclusion": "failure"},
            {"id": 1, "head_branch": "main", "status": "completed", "conclusion": "success"}]}
        with self.assertRaises(ValueError):
            fetch_latest(session, "owner/repo", "2026-09-11")
        self.assertEqual(session.get.call_count, 1)

    def test_download_and_zip_contract(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("result.json", json.dumps(self.payload()))
        self.assertEqual(read_archive(data.getvalue()), self.payload())
        session = Mock()
        run = Mock()
        run.json.return_value = {"workflow_runs": [{"id": 7, "head_branch": "main",
            "status": "completed", "conclusion": "success", "event": "workflow_dispatch"}]}
        artifacts = Mock()
        artifacts.json.return_value = {"artifacts": [{"id": 8, "name": "short-term-screening-7", "expired": False}]}
        download = Mock()
        download.iter_content.return_value = [data.getvalue()]
        session.get.side_effect = [run, artifacts, download]
        self.assertEqual(fetch_latest(session, "owner/repo", "2026-09-11"), (["002988", "600001"], 7))
        download.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

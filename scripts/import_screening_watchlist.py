"""Read a successful main-branch screening artifact; never replace base stocks."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LIMIT = 8 * 1024 * 1024


def candidate_codes(payload, expected_date):
    if payload.get("as_of") != expected_date:
        raise ValueError("选股日期不是最近完整交易日")
    assessment = payload.get("assessment") or {}
    if assessment.get("status") != "ready":
        raise ValueError("选股结果为空或未通过完整核验")
    picks = assessment.get("picks") or []
    if not isinstance(picks, list) or len(picks) > 5:
        raise ValueError("候选数量异常")
    codes = []
    for pick in picks:
        code = str(pick.get("code", ""))
        if not re.fullmatch(r"(?:00|30|60|68)\d{4}", code):
            raise ValueError("候选代码异常")
        if (pick.get("verified") or {}).get("data_date") != expected_date:
            raise ValueError("候选核验日期不符")
        if not 0.5 <= float(pick.get("llm_confidence") or 0) <= 1:
            raise ValueError("候选置信度不符")
        if pick.get("risk_level") == "high":
            raise ValueError("候选风险核验不符")
        if code not in codes:
            codes.append(code)
    return codes


def merge_codes(base, additions):
    # Preserve configured market codes and order; only appended codes are A shares.
    return list(dict.fromkeys([s.strip() for s in base.split(",") if s.strip()] + additions))


def read_archive(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [entry for entry in archive.infolist() if entry.filename == "result.json"]
        if len(entries) != 1 or entries[0].file_size > LIMIT:
            raise ValueError("选股报告附件异常")
        return json.loads(archive.read(entries[0]))


def fetch_latest(session, repository, expected_date):
    api = f"https://api.github.com/repos/{repository}"
    response = session.get(api + "/actions/workflows/short-term-screening.yml/runs",
                           params={"branch": "main", "per_page": 1}, timeout=20)
    response.raise_for_status()
    runs = response.json().get("workflow_runs") or []
    if not runs:
        raise ValueError("尚无选股运行记录")
    run = runs[0]
    if (run.get("head_branch") != "main" or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("event") not in {"schedule", "workflow_dispatch"}):
        raise ValueError("最新选股任务未成功完成，不回用旧名单")
    response = session.get(api + f"/actions/runs/{run['id']}/artifacts", timeout=20)
    response.raise_for_status()
    artifacts = [a for a in response.json().get("artifacts", [])
                 if a.get("name") == f"short-term-screening-{run['id']}" and not a.get("expired")]
    if len(artifacts) != 1:
        raise ValueError("选股附件缺失或过期")
    response = session.get(api + f"/actions/artifacts/{artifacts[0]['id']}/zip", timeout=30, stream=True)
    response.raise_for_status()
    data = bytearray()
    try:
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > LIMIT:
                raise ValueError("选股附件过大")
    finally:
        response.close()
    return candidate_codes(read_archive(data), expected_date), run["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("data/screening-import.json"))
    args = parser.parse_args()
    if args.merge:
        try:
            additions = json.loads(args.output.read_text())["codes"]
            if not isinstance(additions, list) or len(additions) > 5 or any(
                    not isinstance(c, str) or not re.fullmatch(r"(?:00|30|60|68)\d{4}", c) for c in additions):
                additions = []
        except (OSError, ValueError, KeyError, TypeError):
            additions = []
        print(",".join(merge_codes(os.getenv("STOCK_LIST", ""), additions)))
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('{"codes": []}')
    codes = []
    try:
        import requests
        from src.core.trading_calendar import build_market_phase_context, MarketPhase
        ctx = build_market_phase_context(market="cn")
        if ctx.phase == MarketPhase.UNKNOWN or ctx.effective_daily_bar_date is None:
            raise ValueError("无法确认最近完整交易日")
        with requests.Session() as session:
            session.headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
            codes, run_id = fetch_latest(session, os.environ["GITHUB_REPOSITORY"],
                                        ctx.effective_daily_bar_date.isoformat())
        note = f"已导入最近交易日选股 {len(codes)} 只：{','.join(codes)}；来源运行 {run_id}。"
    except Exception as exc:
        # No exception details: request errors may contain signed download URLs.
        note = f"本次未导入短线候选，继续分析原自选股（{type(exc).__name__}）。"
    args.output.write_text(json.dumps({"codes": codes, "note": note}, ensure_ascii=False))
    print(note)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("\n### 短线选股联动\n\n" + note + "\n")


if __name__ == "__main__":
    main()

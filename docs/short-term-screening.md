# 短线收盘选股任务

这是独立的 GitHub Actions 工作流 **A股短线选股**，复用 DSA 内置选股引擎、LLM 配置和通知能力。默认北京时间周一至周五 16:15 启动，交易日检查后生成未来约 2～10 个交易日的观察名单，最多 5 只，可为空。启动时间不等于送达时间。

现有 `00-daily-analysis.yml` 的 10:00 四只自选股分析不受影响；新任务不读写 `STOCK_LIST`，不会自动把候选加入持仓或下单。

## 初版策略

策略入口：`src/services/screening/strategies/short_term_watch.yaml`。这是可解释的试用规则，未完成收益回测或实盘收益验证，名称中的短线不意味着确定收益。

- 快照初筛：排除 ST；价格至少 3 元，成交额至少 3 亿元，总市值至少 30 亿元，PE/PB 为正；换手率至少 2%，量比至少 1.2，单日涨幅 1%～7%。
- 对快照初筛排名前 30 的候选补日 K：价格站上 MA20，技术分至少 60，MACD 非空且为 bullish/neutral；距前 20 日高点 -3%～4%，20 日量比 1.2～4，20 日振幅不超过 30%，ATR20 不超过 6%。参数确切含义以引擎字段和策略文件为准。
- LLM 对有限候选排序，最多返回 5 只；再排除风险等级 high、风险否决、置信度缺失或低于 0.5、风险/退市/新股名称标识、沪深 A 股以外代码。
- 最终候选重新读取日 K：至少 60 根完整、正成交量数据，最后日期必须等于目标交易日；快照价格与当日收盘价偏差超过 0.5% 即不输出该候选。
- 置信度不是胜率；正 PE/PB 不是完整财务审计。没有新闻或公告数据时明确标记未核验，不推断没有利空。策略对新股采用名称标识和至少 60 根日 K 约束，并不等价于完整 IPO 日期筛选。

虽然初始快照来自全市场，日 K 只检查初筛前 30 名；可能漏掉该范围之外的机会。最终名单只含沪深 A 股（包括创业板、科创板，交易需要相应权限），暂不含北交所。其余市场可能出现在源快照覆盖统计中。

## 配置及启用

1. 将 `.github/workflows/short-term-screening.yml`、脚本、策略及文档一起合入默认分支。
2. 复用现有 Repository Secrets：`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`、`PUSHPLUS_TOKEN`。模型名从现有设置读取，没有预置新的付费服务。
3. `TUSHARE_TOKEN`、`TAVILY_API_KEYS`、`BOCHA_API_KEYS` 为可选增强；未配置时有免费行情 fallback，资讯和财务覆盖可能不足。
4. Actions 中打开 **A股短线选股**，如显示 Disabled 则启用。
5. 首次在收盘后手动 Run workflow。非交易日可勾选 `force_run` 验证最近收盘，但不会跳过数据日期检查；盘中拒绝运行。
6. 可勾选 `dry_run` 保存报告但不推送；它仍然取数和调用 AI，可能产生 API 费用。

工作流自身注入 `SCREENING_ENABLED=true`、独立数据目录、禁止 last-good 快照 fallback、快照和日 K 文件缓存 TTL 为 0、日 K 扫描上限 30、2 个日 K worker、单源调用超时和 high 风险否决。无需新增 Secrets；这些值的示例见 `.env.example`。工作流不恢复历史缓存，避免跨日缓存导致旧数据被当作当日数据。

报告状态区分：

| 状态 | 含义 |
| --- | --- |
| 待确认候选 | 已通过本任务核验，仍需下一交易日确认，不是买入指令 |
| 无合适候选 | 数据流程没有已知阻断，规则未留下候选，不放宽规则凑数量 |
| 部分数据未通过核验 | 部分候选核验失败；其余可展示，工作流退出码为 1 |
| 本次筛选不可用 | 快照覆盖不足、过期 fallback、AI 失败或引擎错误；不输出候选，退出码为 1 |

任务将 Markdown 报告和 JSON 核验证据存入 Actions artifact，保留 14 天。运行时限 35 分钟。非交易日正常跳过不会推送。推送请求失败会报错并保留报告；PushPlus API 接受请求不等于微信最终送达，应以实际收件确认。数据源全部失败或行情更新时间不足时不保证能选出股票。

## 验证及回滚

```bash
python -m unittest discover -s tests -p test_short_term_screening_task.py -v
python -m py_compile scripts/run_short_term_screening.py
SCREENING_ENABLED=true python scripts/run_short_term_screening.py --dry-run
```

离线测试覆盖真实策略加载/过滤、陈旧/未来日期、异常价格/成交量、AI 降级、空名单和数据故障的区别、报告转义、推送失败与 artifact 保存。最后一条是在线检查，会取数并使用已配置 AI；需在收盘后执行。

本次初版需在用户 GitHub runner 上完成首次行情+AI+微信联调，不能由离线测试推断实际源可用性或策略收益。

停止任务：Actions → A股短线选股 → Disable workflow。完全回滚可撤销引入此工作流及相关新增文件的提交，原四股跟踪配置无需修改。

本文为个人部署的中文操作说明；不改动现有英文通用说明，因为未改变原有 API 或部署契约。

# GitHub 交易类项目调研（面向 flygate）

- **调研日期**：2026-09-29
- **方法**：7 个并行只读调查代理逐仓库**读源码**（不是读 README 营销文案），必要时用 `gh api repos/<r>/readme`、`gh api repos/<r>/contents/<path>`、`raw.githubusercontent.com` 取文件；星级/许可/最后推送用 `gh api` 逐个核实（少数标 `≈` 的来自仓库页渲染）。
- **范围**：25 个候选仓库，覆盖通用回测/实盘框架、资金费率套利、预测市场、仓位与风控、以及与本项目同源的"果蝇大脑炒币"。
- **一句话结论**：**brain 层没什么可学**（唯一同源的 `fly-trader` 本项目已记录的陷阱的更差版本；`fly-brain` 只有接线先验），但**统计层 / 执行记账层 / 仓位风控层**有三批能直接落地的东西——且**都不需要先有一个 edge**。

> ⚠️ 本文 §4 的两处自身缺陷基于**本副本**（`E:/Claude/github/flygate`，HEAD `637c996`，工作区干净）。真机（Mac mini M4）上若已修，忽略该节。

---

## 0. 结论摘要

1. flygate 的头号问题（README 自陈）是"**没有跑赢'少动'本身**"——这是**统计问题**，不是模型问题。`machine-learning-for-trading` + `qlib` 给出完整答案，且 flygate 现有 7 臂×5 种子的输出**正好是** DSR/PBO 需要的输入。这是全调研中收益/成本比最高的一件事。
2. 执行真实性与仓位/风控三条（jesse / freqtrade / chrisleekr / pysystemtrade / nautilus / hftbacktest）**不需要 edge 就能做**，且都是纯 Python/numpy，不引入依赖。
3. **不要**引入任何整体框架（NautilusTrader / qlib / freqtrade / hummingbot / Lean）作为运行时——它们的工程复杂度都在解决 flygate 刻意没有的问题（被动限价单、L2/L3 订单簿重放、亚秒延迟、多所 OMS）。
4. **不要**把资金费率套利并进引擎：零售 60U 上限下成本地板算不通（见 §3.4）。
5. **不要**动 Polymarket 执行；雷达留下但要改费率模型（见 §4.2）。

---

## 1. 分级清单

### A. 必读（直接解决 flygate 已承认的短板）

| 仓库 | ★ | 许可 | 最后推送 | 价值 |
|---|---|---|---|---|
| `stefan-jansen/machine-learning-for-trading` | 21,105 | MIT | 2026-09-28 | 唯一能回答"到底有没有 edge"的一层：purged/embargoed CV、CPCV、Deflated Sharpe、PBO(CSCV)、PSR/MinTRL、triple-barrier、meta-labeling。numpy+scipy 内联实现可直接抄 |
| `freqtrade/freqtrade` | 54,887 | GPL-3.0 | 2026-09-28 | 参考实现：FreqAI 滚动重训 + 三重 `do_predict` 信任门；`lookahead-analysis`/`recursive-analysis` 对抗性自检；dry-run 的 L2 走单成交模型；`plugins/protections/*` 逐对风控锁 |
| `jesse-ai/jesse` | 8,593 | MIT | 2026-09-28 | 无未来函数的**结构性**保证：`candle[0]+60_000 <= available_at`；`split_candle` 让止损在真实价位成交；跳空穿越单匹配；按 fill 计费 |
| `eslazarev/purged-cross-validation` | 35 | MIT | 2026-09-27 | sklearn 兼容的 purged / CPCV / DSR 即用包（含 `paper/`、`CITATION.cff`）。只想拿统计、不想读整本电子书时用它 |

### B. 按需（局部机制好，整体不引入）

| 仓库 | ★ | 许可 | 最后推送 | 拿走什么 |
|---|---|---|---|---|
| `pst-group/pysystemtrade` | ≈3.5k | GPL-3.0 | 2026-09-19 | `robust_vol_calc`（EWM(35) + 500 日 5% 分位地板）+ `risk_overlay.get_risk_multiplier`（连续降杠杆，而非 20% 断崖） |
| `chrisleekr/binance-trading-bot` | 5,567 | Apache-2.0 | 2026-09-09 | `sizing.ts::resolveEntryBudget`：`desired = percent × equity / stopDistance`；`stop-level.ts` 单一 stop 解析器（治"三处各自取整"那类 bug） |
| `nkaz001/hftbacktest` | 4,792 | MIT | 2025-12-23 | 只取两件：maker/taker 分离费率；**把 (请求, ack, 终态, 成交均价) 测下来存进 Trade**——不是队列模型 |
| `nautechsystems/nautilus_trader` | 29,482 | LGPL-3.0 | 2026-09-29 | `OrderDeniedReason` 式**拒绝原因枚举 + 计数**；`calculate_fixed_risk_position_size`（按止损距离定量）；mark price 新鲜度不变量 |
| `microsoft/qlib` | 49,015 | MIT | 2026-09-22 | `SignalRecord`/`SigAnaRecord`(IC) 与 `PortAnaRecord`(PnL) **分离成两个产物**；`MultiPassPortAnaRecord(shuffle_init_score=True)` 洗牌信号零假设 |
| `hummingbot/hummingbot` | 20,248 | Apache-2.0 | 2026-09-28 | 资金费率**单位归一**（`get_normalized_funding_rate_in_seconds` + `funding_payment_interval_map`）、`next_funding_utc_timestamp` 结算时点、按费率差而非价格的离场止损 |
| `warproxxx/poly-maker` | 1,508 | MIT | 2026-09 | 纯函数两侧报价 `quoting.py`（买卖都是 bid → 成交即 merge 锁 edge）；`scoring.py` 返佣池估算；**实测微观结构样本**在 `config/strategy.toml` |
| `Jon-Becker/prediction-market-analysis` | 3,864 | MIT | 2026-09 | 按**成交时刻**算 Brier/ECE/log-loss（快照式会给出虚假 0.05 vs 真实 0.17）；maker/taker 角色分解；可恢复的区块回填设计 |
| `evan-kolberg/prediction-market-backtesting` | 1,206 | MIT+LGPL | 2026-05 | Polymarket 真实费率式 `fee = qty·rate·p·(1−p)`、maker rebate 20/15/25%、`StaticLatencyConfig(75/10/5/5 ms)`；以及免费 **PMXT 逐小时 L2 订单簿档案**（2026-02-21T16:00Z 起） |

### C. 忽略 / 陷阱（写明理由，省得重新踩）

| 仓库 | ★ | 否决理由（基于源码，不是印象） |
|---|---|---|
| `sngx762/fly-trader` | 3 | 唯一"同行"（果蝇蘑菇体 + 多巴胺 RPE），但**是本项目已记录陷阱的更差版本**：KC→MBON 只有**一个**带符号标量乘进一个 256×16 全非负矩阵（`W_MIN=0` 永不可抑制），无动作归属、无资格迹滞后（决策 t 的收益记到图案 t+1），`tanh(10×美元盈亏)` 在满仓账户上饱和成 ±1 符号位；`RPE_GAMMA`/`DA_DECAY` 是死配置（非 TD）。数据侧：硬编码 seed=42、同一段数据边学边交易、**零手续费、无 buyhold 基准**；headline 从 +17.24% 改到 +11.54% 而大脑文件没动。只值得拿两个原语，见 §2.3 |
| `eonsystemspbc/fly-brain` | 926 | 真项目、真 benchmark，但**整个仓库没有蘑菇体、没有任何可塑性**（权重一次性从连接组设定，无 STDP、无 DAN）。GPL-2.0 + conda/CUDA/NEST-GPU + 138k 神经元/5M 突触。只取接线先验与消融方法论，见 §2.6 |
| `godzilla-foundation/godzilla-community` | 376 | **仓库里根本没有资金费率套利策略**（9,349 个路径中 `funding*` 命中 0；白皮书 §4 明说 funding validation / 双腿规模 / 部分成交处理"有意不公开"）。可用的只有 `trap_screener.py` 与 `binance_delisting_monitor.py`，见 §2.6 |
| `supervik/funding-rate-arbitrage-scanner` | 28 | 无 LICENSE、无费用/滑点、7 次提交、2024-05 停更；其"机会"会把净 carry 高估掉整个往返成本 |
| `IrakliXYZ/ARBOT` | 38 | 无 LICENSE，FTX 专用，最后提交 2021-11-23，**已死**；双腿失败互不知情（`controller.py` 分别 try/except 吞异常） |
| `aarora4/Awesome-Prediction-Market-Tools` | 762 | 无 LICENSE；通篇 `?utm_source=polymark.et` 联盟链接，开头就是广告位，内容未审 |
| `hudson-and-thames/mlfinlab` | ≈4.9k | **all-rights-reserved**；`bet_sizing/bet_sizing.py` 与 `ch10_snippets.py` 里**每个函数体都是 `pass`**。别装 |
| `QuantMuse` / `pybroker` / RL 类（FinRL、TradeMaster…） | — | 在 DSR/PBO 到位前上 = 放大多重检验；见 §5 第 1 条 |

### D. 名气压过相关性（建议不碰，附一句理由）

| 仓库 | 一句否决 |
|---|---|
| `polakowo/vectorbt` ★9.2k | 向量化参数扫描。flygate 的问题是 7 个臂比不出差异，不是跑得不够快 |
| `QuantConnect/Lean` ★21.8k | C#/.NET + 股票/期货数据模型；只有两个回撤公式值得当数学抄（见 §2.5） |
| `hummingbot` 作为运行时 | 第二个运行时 + 双腿 OMS + cython 事件/订单追踪；且 `hummingbot/strategy/spot_perpetual_arbitrage/` **完全不读 funding**（费率套利在 `scripts/v2_funding_rate_arb.py`，ScriptV2 脚本） |
| `freqtrade/freqtrade-strategies` ★5.5k | 策略动物园（talib 指标配方），无 CI、无验证；维护者自己提交 "remove missleading statistics" 删掉了业绩表。只有 4 个机制值得移植，见 §3.2 |
| `Lumiwealth/lumibot` ★2.1k | 真有 Polymarket 执行（`lumibot/brokers/polymarket.py` 1342 行），但 **GPL-3.0 传染** + 12 券商的通用框架 |
| `godzilla` 的 ~125µs 延迟 | mock 端点、journal-only 单机软件微基准，与成交/盈利无关；flygate 决策周期是一根 1 分钟 K，差 4–5 个数量级 |

---

## 2. 按 flygate 短板映射的可落地机制

### 2.1 短板 #5（统计弱）——**优先级最高**

**来源**：`stefan-jansen/machine-learning-for-trading`

| 机制 | 源码路径 | 精确接口 |
|---|---|---|
| Deflated Sharpe + PBO(CSCV) | `16_strategy_simulation/12_dsr_validation.py` | `deflated_sharpe_ratio(observed_sharpe, skewness, kurtosis, n_samples, n_trials, variance_trials, confidence_level, return_format, ...)`、`_expected_max_sharpe`、`probabilistic_sharpe_ratio`、`cscv_sharpe_matrices(strategy_returns, n_blocks)`、`compute_pbo(is_performance, oos_performance)`；`n_blocks` 扫 `[4,6,8,10,12,16]` |
| Sharpe 不确定度 / MinTRL | `16_strategy_simulation/11_sharpe_ratio_inference.py` | `sharpe_ratio_variance`、`sharpe_ratio_se`、`lo_2002_annualized_sharpe(returns, periods_per_year, max_lag)`（自相关校正）、`compute_min_trl` |
| Purged + embargoed 走查 / CPCV | `06_strategy_definition/02_cv_foundations.py:954`、`utils/cv_splits.py` | `CombinatorialCV(n_groups=6, n_test_groups=2, label_horizon=5, embargo_size=2)`；`generate_cv_splits(..., label_buffer="21D")`、`_purge_holdout_touching_validation(...)` |
| 复杂度惩罚（可选） | `16_strategy_simulation/13_ras_protocol.py` | `rademacher_complexity`、`ras_sharpe_adjustment`、`massart_upper_bound` |
| HAC 校正的 IC 推断 | `07_defining_the_learning_task/06_ic_inference.py` | `compute_ic_hac_stats(ic_series, label_horizon=21)`、`block_bootstrap_ic_ci(...)`、`min_track_record_ic(...)` |

**映射到 flygate**：
- flygate 已经产出 7 臂 × 5 种子的收益序列 —— **这就是 DSR/PBO 的输入**，不需要新数据。
- "多巴胺开启时 +0.3pp 更差" → 变成 `P(DSR)` 与"样本内冠军落到样本外中位数以下的频率"。
- 对臂间配对差做 `paired_diff` 的 HAC t 统计 + `min_trl`，回答"几周 1m 数据根本不足以区分"。

**成本**：约 120 行 numpy + `scipy.stats.norm`（`cdf`/`erfinv`）。**零新依赖、零新数据、不碰交易代码。**

**配套（qlib 架构层，非统计量）**：`qlib/workflow/record_temp.py` 把 `SignalRecord`(预测/标签) / `SigAnaRecord`(IC) / `PortAnaRecord`(PnL) **拆成不同产物**。flygate 的 `eval/protocol.py` 输出的 8 个指标**全是路径统计**，因此三种失败不可区分：(a) tilt 无信息、(b) tilt 有信息但被闸门/阈值丢掉、(c) 被仓位与暴露毁掉。再加 `MultiPassPortAnaRecord(pass_num=10, shuffle_init_score=True)` 的洗牌零假设做对照。
> ⚠️ qlib 的 IC/ICIR/RankIC 假设横截面宽度（每日数千标的）；flygate 只有 2–4 对，rank IC 退化。**可迁移的是产物分离，不是 IC 统计量。**
> ⚠️ `paperswithbacktest/awesome-systematic-trading`（★14.5k，**无 LICENSE**）引用复现记录：4,843 篇论文中位复现 Sharpe 0.37，仅 48% 过 t=1.96，中位 beta +0.17 → 剔暴露后中位 IR 0.21；经验法则 **年数 ≈ (1.96/Sharpe)²**（Sharpe 0.4 需约 24 年）。该列表本身是商业漏斗（付费层、门控数据），只能取其方法学陈述与条目名。

### 2.2 短板 #3（执行不真实）

| 机制 | 来源 | 内容 |
|---|---|---|
| 日内切 K 出真实止损价 | `jesse/modes/backtest_mode.py::_simulate_price_change_effect` + `jesse/services/candle_service.py::split_candle` | 把形成中的 K 按订单价位切开，在切点标记仓位；现 flygate 全部按下一根开盘成交（`backtest.py` 自述），止损要等一整根 |
| 跳空穿越匹配 | `jesse/modes/backtest_mode.py::_order_is_crossed_by_opening_gap` / `_execute_opening_gap_orders` | 挂单仅在 `min(prev_close, open) ≤ price ≤ max(prev_close, open)` 才成交；多单同时穿越时按对己方最差顺序成交 |
| 按 fill 计费 + 部分成交 | `jesse/services/order_service.py::execute_order` | `fee = fee_rate × abs(filled_qty) × price`，`filled_qty/remaining_qty` 记账 |
| 观测边界不变量 | `jesse/services/candle_service.py::generate_completed_candles_from_observed_minutes` | 1m 行仅在 `candle[0] + 60_000 ≤ available_at` 时可见；图表窗口最新一根绝不能是形成中的 K |
| L2 走单纸面成交 / maker-taker 分离 | `freqtrade/exchange/exchange.py::get_dry_market_fill_price` / `add_dry_order_fee` / `_dry_is_price_crossed` | 20 档走单、封顶 5% 滑点；立即成交按 taker、挂单按 maker 计费 |
| 实测延迟三元组 | `nkaz001/hftbacktest` 的 `IntpOrderLatency` / `hbt.order_latency()` | 思想而非代码：**测** `(req_ts, exch_ts, resp_ts)` 而不是建模。flygate 的 `GateBroker._settle` 已经观测到 request→ack→终态却把时间丢了 |
| 拒绝原因枚举 + 计数 | `nautilus crates/risk/src/engine/mod.rs` 的 `OrderDeniedReason` | 现在 `RiskManager.allow() -> (bool, str)` 的自由文本，变成枚举 + 逐原因计数，让"为什么被拒"可测量 |
| mark price 新鲜度不变量 | `nautilus crates/portfolio/src/portfolio.rs` | 推广 flygate 自己的 `anchor_ready`（部分锚点曾静默解除日亏熔断） |

**成本**：低（纯 Python）。⚠️ 这些是**成本**不是收益：诚实化之后回测数字**会变低**。jesse 没有滑点模型——flygate 的 `RiskConfig.slippage` 在这一项上反而领先。

### 2.3 短板 #2（多巴胺通道惰性/有害）

三层，从治本到补丁：

1. **调度式滚动重训**（`freqtrade/freqai/data_kitchen.py` + `freqai_interface.py`）
   `split_timerange` 造出成对的训练窗/紧邻回测窗；`purge_old_models` 只留最新 N 个 `sub-train-<COIN>_<ts>`；`check_if_new_training_required` 以 `live_retrain_hours` 触发；`startup_candle_count*2` 预热防 NaN 进拟合。→ 把"逐观测多巴胺"换成**滚动窗口上的计划重训 + 紧邻评估窗**。
2. **三重信任门**（同仓库）
   `do_predict ∈ {0 中性, 1 ok, 2 过期}`，过期由 `check_if_model_expired(trained_timestamp)` vs `expiration_hours` 判定；分布外用 `define_data_pipeline` 的 `VarianceThreshold → MinMaxScaler → SVMOutlierExtractor → DissimilarityIndex(di_threshold)`，`DI_k = d_k / d̄`。→ **结构上让"1 分钱盯市噪声在教大脑"不可能发生**（当前通道 0–5 次/天，且开着有害）。
3. **两个原语**（`sngx762/fly-trader`，只拿这两个）
   - `DopamineSystem.compute_rpe`：`rpe = actual_reward − EMA(α=0.1)`，收益做无量纲化 → 取代"固定绝对死区"（flygate 的刀刃：倍数 0.0 时 44–98 次/天噪声，3.0 时 0 次）。
   - `TradingAgent.update_readout`：`effective_lr = lr × da_level / DA_BASELINE` —— 幅值当学习率增益，而不是塌缩成 reward/punish 两个布尔。
   - 形态建议：`rpe = (equity_delta / order_notional) − EMA`，**保留 `dopamine_threshold()` 的手续费地板作为事件触发器**，但尺度变无量纲。
   - ⚠️ 它的 `FreqAI.continual_learning` 类比是陷阱：freqtrade 文档自己警告"高概率过拟合/陷入局部最优"。

### 2.4 短板 #4（无组合/仓位）

| 机制 | 公式 | 来源 |
|---|---|---|
| 固定分数风险定量（**首选**） | `stopDistance = 1 − stopLoss`；`desired = percent × equity / stopDistance`；再夹 `min(0.5×equity, free_quote, capQuote − deployed)` | `chrisleekr/binance-trading-bot:packages/strategy/trailing-trade/src/sizing.ts` |
| 波动率目标定量 | `order_notional = equity × target_vol / vol_est`；`vol_est = EWM(span=35, min_periods=10).std(log_returns)` 并在地板 `rolling(500).quantile(0.05)` 处取大 | `pst-group/pysystemtrade:systems/positionsizing.py` + `sysquant/estimators/vol.py::robust_vol_calc` |
| 连续降杠杆（取代断崖） | `m_i = limit_i / max(limit_i, risk_i)`，取 `min` 后乘到所有仓位 | `pst-group/pysystemtrade:systems/risk_overlay.py::get_risk_multiplier` |
| 按止损距离定量（nautilus 版） | `calculate_fixed_risk_position_size(instrument, entry, stop_loss, ...)` | `nautilus crates/risk/src/sizing.rs` |

**映射**：直接替换 `RiskConfig.order_notional = 10.0`（现在 ATR 0.2% 和 3% 下注完全一样——README 表格里 PONS −11.1% 与 BTC 同额就是这个原因）。② 的分子 `forecast/avg_abs_forecast` **不要抄**，那是"已证明的 edge"机器，flygate 没有。
**HRP 无收益权重**（`PyPortfolio/PyPortfolioOpt:pypfopt/hierarchical_portfolio.py::HRPOpt.optimize`，`alpha = 1 − V1/(V1+V2)`）不需要收益预测，但在 3 个高相关主流币 + 10U 单调粒度下权重会取整成等权【推断】——等币种数或预算变大再回头看。

### 2.5 退出卫生（`trader/risk.py`）

| 机制 | 来源 |
|---|---|
| 开仓相对止损的闭式解：多头 `stoploss = 1 − ((1 + open_relative_stop/leverage) / (1 + current_profit))`，`max(·,0)` | `freqtrade/strategy/strategy_helper.py::stoploss_from_open` |
| 不变量"**止损只能上移，永不回撤**"；按交易所 tick 取整（多头 `ROUND_UP`）；审计行 "trailing stoploss saved us" | `freqtrade/persistence/trade_model.py::Trade.adjust_stop_loss` |
| 止损/止盈哨兵按"**仓位市值**（qty×price）"而非价格记高点——修规模减半后 ratchet 高估的问题 | `Lean:Algorithm.Framework/Risk/TrailingStopRiskManagementModel.cs` |
| 名义加权有效止损价 `Σ\|qty·price\| / Σ\|qty\|` | `jesse/strategies/Strategy.py::average_stop_loss` |

### 2.6 短板 #1（决策只由一个标量决定）

- **结构性自检**（`freqtrade`）：`optimize/analysis/lookahead.py` 全量跑一遍后，对每笔非强平交易把数据切到 `open_date + 1 candle` 复跑，统计 `false_entry/false_exit`，并用 `DataFrame.compare` 标出 `biased_indicators`；`recursive.py` 以 startup 计数 `{199,399,499,999,1999}` 扫指标漂移。配套负向夹具：`freqtrade-strategies/user_data/strategies/lookahead_bias/{DevilStra,GodStraNew,Zeus,wtc}.py` 是 4 个**故意有偏**的策略。
  → 对 flygate：**"改种子/改接线而决策位级不变"是 bug 信号，不是零结果。** 一个对整天数据做归一化的 mutant 渲染器必须让评测器报出不同数字——今天什么都不会失败。
- **接线先验**（`eonsystemspbc/fly-brain`）：`data/2025_Connectivity_783.parquet`（100,804,642 B，列 `Presynaptic_Index`/`Postsynaptic_Index`/`Excitatory x Connectivity`），`code/run_pytorch.py::get_weights()` 是现成的 parquet→稀疏矩阵配方。用作 PN→KC 固定接线可**直接消灭"随机接线导致种子无效"的混淆**。⚠️ 细胞类型不在该仓库（`2025_Completeness_783.csv` 只有 `flywire_id, Completed`；唯一的类型标注 `data/sez_neurons.pickle` 只覆盖 SEZ），要自己 join FlyWire v783 分类；且仓库 GPL-2.0 —— **从上游公开数据重建子矩阵比 vendor 这个 parquet 更干净**。
- **群体沉默式因果消融**（同仓库 `model.py::poi()` / `silence()` + `utils.get_rate()`）：静默一组指定神经元后测下游放电率变化。flygate 现有 `rewired`/`frozen` 只翻开关，不是测通路。
- **新信息轴**（`godzilla trap_screener.py::score_row()`）：OI/mcap（>0.5 警告 / >1.0 危险）、perp/spot 24h 量比（>15 / >40）、`abs(funding_apr)`（>100% / >300%）、上市天数（<180 / <60）、市值（<$100M / <$30M）。全部免费公开端点。
  ⚠️ **没有任何被调研的仓库证明 funding 符号能预测现货方向**（四个都在赚 carry 的 P&L）——所以它只能作为 `eval/protocol.py` 里的独立消融臂，与 `rewired`/`random` 同等待遇，**不能当作假定的改进**。
- **退市/插针看门狗**（`godzilla strategies/binance_delisting_monitor/`）：去重状态文件只吐新命中、多主机回退、`DELIST_PATTERNS`/`EXCLUDE_PATTERNS` 排除"取消退市/恢复交易"、`NOT_TICKERS` 白名单。flygate 完全没有——而 README 自己写着"新上架币（如 PONS）注意插针风险"。

---

## 3. 其他战场（决定不并入引擎的部分）

### 3.1 预测市场（Polymarket）

- **真实费率**（官方 2026-09）：taker `fee = 份额 × rate × p × (1−p)`，5 位小数，maker = 0。类别 rate：加密 0.07、体育 0.05、金融/政治/科技 0.04、经济/文化/天气 0.05、**地缘政治/世界事件 0**。→ 政治类 p=0.5 双边约 2.0%，加密约 3.5%，地缘政治免费。
- **成本地板**：10U 单边持有到期，政治类 ≈$0.20、加密 ≈$0.35；往返翻倍。返佣/激励全部在零售规模之上：Maker Rebate 有 **$1/日 最低结算额**（10 份额 @0.50 的政治单只赚 $0.025 信用 → 需约 40 笔/日才够门槛）；Liquidity Rewards 样本市场要求 **≥100 份额（@0.5 = $50 名义）/ ≥50 份额**，单笔就吃满预算；Taker 返佣档位从 **$2,000 / 30 天加权量** 起，10–60U 账户是 0%。
- **结论**：`strategies/polymarket_radar.py` **保留为雷达，永不执行**。已记录的财富转移方向是 taker→maker（`Jon-Becker` 的 Kalshi 数据显示 taker −1.12% / maker +1.12%，且这个差值**是专业做市商进场后才出现的**）。
- ⚠️ `Jon-Becker` 论文里所有量化数字（7,210 万笔、$182.6 亿、长尾 EV 等）都是 **Kalshi** 的，不是 Polymarket。别当 Polymarket 证据引用。
- **白拿的数据**：PMXT 逐小时 Polymarket L2 订单簿档案（`archive:r2.pmxt.dev`，2026-02-21T16:00Z 起，免费无 key，Parquet，`make download-pmxt-raws` 增量可续）——这是**唯一公开的亚分钟级订单簿状态**，可直接攻击短板 #3/#6。先拉一小段（几个高量市场 × 几周），别全量镜像；R2 镜像的许可条款未核实。

### 3.2 freqtrade-strategies 里仅有的 4 个机制

1. **波动感知切片**（`AlmgrenChrissStrategy.py::_ac_kappa_series`）：`kappa` 由 `sigma`、`avg_spread/avg_volume` 推出的 `eta_tilde`、`gamma` 组成，`raw_kappa = arccosh(0.5·κ̃²·τ² + 1)/τ`，切片刻度 `1 − sinh(κ(m−1))/sinh(κm)`（κ→0 退化为 TWAP）。→ 给 flygate 的固定 10U 一个免费**波动率标量**。
2. **时间切片进出**（`TWAPStrategy.py`）：`custom_stake_amount = proposed_stake/twap_num_slices`，按 `last_fill_time + twap_interval_minutes` 重切，退出返回**负**切片——这也是让部分成交建模变得有意义的机制。
3. **含手续费的 R 倍数阶梯**（`FixedRiskRewardLoss.py`）：`stoploss_rate = close − 2·ATR`，1R 处移到不亏（`open_rate·(1+fee_open+fee_close)/current_rate − 1`），3.5R 转跟踪。→ flygate 现在从不记录 R 倍数，有了它协议才有可自举的分布。
4. **负向夹具**（`user_data/strategies/lookahead_bias/`）：见 §2.6。

### 3.3 资金费率套利：成本地板（**收盘结论：不并入**）

假设来自 flygate 自己的配置：Gate 现货 taker 0.2%/笔、USDT 永续 taker 0.05%/笔、现货滑点 0.05% / 期货 0.03%。carry 往返 = **4 笔成交**。

| 往返成本 | 构成 | 平衡所需结算次数 @0.01%/8h | 天数 |
|---|---|---|---|
| 0.20% | 仅按 0.2% 往返 | 20 | 6.7 d |
| 0.40% | 现货 2×0.2% | 40 | 13.3 d |
| 0.50% | 双腿 taker：2×0.2% + 2×0.05% | 50 | 16.7 d |
| **0.66%** | 双腿 + 滑点 | 66 | **22.0 d** |
| 0.24% | **全 maker**（2×0.1% + 2×0.02%）【推断：Gate maker 费率】 | 24 | 8.0 d |

`N = RT / r` 与规模无关。但绝对收益：60U 上限下双腿对冲名义 ≤30U → 毛 = 30×0.03%/日 ≈ **$3.29/年**；一次进出 0.66% = $0.198 ≈ 22 天毛收入。最好情况净 **$3.09/年 = 5.2% APR**，而 flygate 的日亏熔断是 $12（60U 的 20%）——**一年的 carry 只有一次熔断的 25%**。1% 基差不利 = $0.30 = 33 个资金日；README 自述新上架币 3% 跳空 = $0.90 = 100 个资金日。
> 唯一能负担的区间是"全 maker（RT ≤0.24%）+ 持续 ≥330% APR 资金费率"——**而这正是 `trap_screener` 判为危险的同一区间**（OI/mcap>1、perp/spot 量比>40、上市<60 天、|funding APR|>300%）。费用地板恰好在挤兑风险最高处才可负担。

**裁决**：
- `strategies/funding_radar.py` → **保留并升级，但保持不接入决策路径**：① 统一间隔归一（`hummingbot get_normalized_funding_rate_in_seconds` / `50shadesofgwei normalize_funding_rate_to_8hrs`）② 加 `trap_screener` 那几列 + `supervik` 的 `mean_daily_amplitude` ③ 持久化 `next_funding_time`。
- `strategies/arb_sim.py` → **保留模拟器，忽略其可执行性**：① 把通过判据换成"**平衡所需资金日数** `N = RT/r`" ② 用**按结算事件计数、带符号**的 PnL 替换 `sum(rates) − fee_rt` ③ 尾部换成预测式漂移退出 ④ `FEE_RT` 记成 maker/taker 两档。
- 完整 carry 账本（`50shadesofgwei` 的 `MatchingEngine → checkProfitability → TxExecution/*PositionController → PositionMonitor` + `TradeDatabase`，`hummingbot` 的 `FundingRateArbitrage` 状态机）→ **属于另一个策略族**，需要永续腿、保证金、2× 资金与双腿原子性。
- ⚠️ `50shadesofgwei` 的 `is_position_delta_within_bounds()` 是**死代码**：`relative_delta = |p1−p2|/(|p1|+|p2|) ∈ [0,1]`，故 `relative_delta − 1 ≤ delta_bound` **恒真**，宣称的 delta 保险永不触发。别抄双腿健康检查。
- ⚠️ `godzilla` 的延迟数字是 mock 端点、journal-only 的单机微基准，论文自己写明"不衡量策略盈利/成交概率/队列位置/交易所延迟"。

---

## 4. 顺带发现的 flygate 自身缺陷（已核对本副本源码）

### 4.1 `flygate/scanner.py:114-119`

```python
fund_day = abs(float(tk.get("funding_rate") or 0)) * 6   # 4h -> daily
taker = float(spec.get("taker_fee_rate") or 0)
spread = (cd["ask"] - cd["bid"]) / cd["last"]
energy = coh85 * rng
cost = fund_day + 2 * taker + spread                 # round-trip drag
```

两个问题：

1. **硬编码 4h 间隔**。同仓库的 `strategies/funding_radar.py:49` 已经正确读 Gate 合约的 `funding_interval`（默认 28800 s = 8h）；scanner 却写死 `×6`。对默认 8h 合约应 `×3` → **持有成本被高估 2 倍**，直接压低 `fit` 排序。
2. **`abs()` 抹掉符号**。做多永续时正费率是成本、负费率是**收入**；取绝对值让"做多还能收钱"的合约同样受罚。

（scanner 是期货适配扫描器，把 funding 计入往返成本本身是对的；问题是口径。）

### 4.2 `strategies/polymarket_radar.py`

`FEE = 0.018` 是**类别盲 + 价格盲**常数，`THRESH = 1 − FEE − 0.005`。按官方 2026-09 的 `rate·p·(1−p)`：

- 加密类 p=0.5 双边 **3.5%** → 按 1.8% 算会**假阳性**（`cost = 0.97` 看着可赚，实际约 −1.5 分/份）；
- 地缘政治/世界事件 **0%** → 按 1.8% 算会**假阴性**，恰好漏掉最干净的一类；
- 没有除以 `endDate` 剩余天数：同样的 2.35% 是 28%/年还是 3%/年取决于期限；
- 深度只在 `cost ≤ 0.977` 时记、只记最优价、且不记报价时间戳；1h 采样**无法分辨它声称要测的"持续时间"**（同一条 0.977 挂单在一小时后仍在，更可能是陈旧档位/最小份数/一侧打不到，而不是白送的午餐）。

补法：读 Gamma 的 `feesEnabled` / `feeSchedule.rate` / `feeSchedule.rebateRate`；按 `(1 − cost)/days_to_resolution` 排序；两侧都记深度与时间戳；候选出现时开 burst 模式（如 5–10 s 轮询 5 分钟）或从门径里删掉"持续时间"。

（`strategies/README.md` 实验 #3 的原文假设"扣 1.8% 费后仍有双边/互斥组价差可扫"、门径"7 天内机会 ≥5 次且腿深度 ≥200 份"已核对。OKX 4h 合约按 8h 年化的问题 `funding_radar.py` 自己的 docstring 已承认——补法就是该端点本来就返回的 `fundingTime`/`nextFundingTime` 差值。）

### 4.3 与自身认知一致的部分（不必改）

- `PaperBroker`/`FuturesBroker` **确实**按笔建模了滑点与 taker 费（`px = price×(1 ± slippage)`、`fee = notional × taker_fee`）——缺的是**未确认成交、队列位置、日内路径、部分成交**。
- `eval/protocol.py` 的 bootstrap 置信区间 + `paired_diff` + **暴露匹配扣费基准 `bh_matched`** 已经比 freqtrade 的 `market_change`、jesse 的 `metrics.py`、hftbacktest 的 `stats.metrics`、nautilus 的 `statistics/*` 都更贴合自身问题。**别用那些库替换它，只往上加 DSR/PBO。**

---

## 5. 建议落地顺序

1. **DSR + PBO(CSCV) 跑在现有 7 臂 × 5 种子格子上** —— 收益最高、成本最低，且让后续所有尝试可判定。
2. **臂间配对差的 beta 匹配 IR + HAC t 统计 + MinTRL** —— 把"跑赢持有"里的暴露算术剥掉。
3. **purged/embargoed + CPCV 重采样** —— 前两条的底座，同时把"种子无效"从断言变成分布式结论。
4. **按止损距离定量 + 连续降杠杆** —— 不需要 edge，立刻改善 PONS 式单币亏损。
5. **jesse 式日内切 K / 跳空匹配 + 逐单延迟-滑点记录** —— 诚实化回测（**数字会变低**）。
6. **多巴胺通道：先上信任门**（`expiration_hours` + 分布外拒绝），再谈是否值得改重训调度。
7. 雷达侧：`scanner.py` 间隔/符号、`polymarket_radar.py` 费率与年化、`funding_radar.py` OKX 间隔。（都是小改，互不影响引擎。）

---

## 6. 未验证 / 边界

- 星级、许可、推送日期用 `gh api` 逐个核实（除 `pysystemtrade`、`PyPortfolioOpt`、`mlfinlab` 标 `≈` 者来自仓库页渲染）。
- `freqtrade-strategies`、`awesome-systematic-trading`、`PyPortfolioOpt`、`mlfinlab` 等**未逐文件验证 GPL/许可边界**；**不要 verbatim 复制 GPL-3.0 代码**（freqtrade、pysystemtrade、bittrader 系）与 LGPL-3.0（nautilus）——概念重写没问题，成段拷贝是许可陷阱。
- `pysystemtrade` 的 `forecast/avg_abs_forecast` 对 flygate 无意义【推断】；3 个高相关主流币下 HRP 退化为反方差【推断】；Gate maker 费率 0.1%/0.02%【推断】。
- 本文所有仓库侧结论都取自被读取的源文件路径（各节已标）；标 `【推断】` 者为未直接验证的推论。

## 附：本次未收录的候选（一句话）

`vectorbt` 向量化扫描器 / `Lean` C# 股票期货 / `Krypto-trading-bot` 2024-12 停更 / `binance-trading-bot`(chrisleekr) 只取 2 个 TS 文件 / `Hummingbot` 作为运行时 / `FinRL`·`TradeMaster`·`RLQuant` RL 系在 DSR/PBO 到位前是多重检验放大器 / `octobot`·`Superalgos`·`crypto-signal` 通用机器人、无 edge 证据。

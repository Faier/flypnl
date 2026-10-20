# FlyGate 统一架构：果蝇网络 × Jev × 现货/合约/Polymarket（一个项目）

> 定位：**宿主项目只有一个 = flygate**。本包是"缺的模块"，合入真机仓库后，
> 所有能力都在 flygate 里：`flygate/decide/`（Jev 决策层）、`flygate/markets/`
> （Polymarket 腿）。现货与合约引擎、风控、账本、协议统计**原样不动**。
> A股腿已移除：T+1 + 无执行通道 + 无数据源，日级筛选不构成本系统-needed 能力；
> 将来要做再按 git 历史找回（commit 移除 ashare.py 的那笔）。

## 1. 一张图

```
                         ┌────────────────────── flygate（唯一项目）──────────────────────┐
                         │                                                                │
 现货腿 Engine ──┐        │  编码层              读出/决策层              执行/测量层        │
 合约腿 Futures ─┼─观测──▶│  果蝇网络(MaleCNS)   flygate/decide/          RiskManager       │
 Polymarket 腿 ──┘        │  渲染→tilt/R²/KC稀疏  jev.py(缓存/钉版本)      Broker(纸/实)     │
                         │  →tel 遥测 dict      state.py(去标识)         eval/protocol     │
                         │                      questions 模板        DSR/PBO/MinTRL     │
                         │                           │                shadow 孪生/CF账本  │
                         │                     choice/score/noul ◀──┘                    │
                         └────────────────────────────────────────────────────────────────┘
```

> Polymarket 腿**不走果蝇网络**（事件市场没有趋势图），Jev 直接定价；编码层只服务现货/合约。

三条市场腿共享同一套东西：**编码（可选）、决策（可选 Jev）、风控、账本、统计协议**。
差别只在数据源与决策频率。

## 2. 各腿规格

| 腿 | 数据源 | 决策频率 | 果蝇网络 | Jev 落点 | 本包交付 |
|---|---|---|---|---|---|
| 现货 | Gate 1m（引擎现有） | 60s 观测 | 是（特征） | ①方向/强度 ②门 ③教学归因 | decide/ 全部 |
| 合约 | Gate 永续（引擎现有） | 同现货 | 是 | ①②+仓位强度（杠杆需连续缩放） | 复用 decide/ |
| Polymarket | 公开事件 API（自接） | 事件驱动 | **否**（无趋势图） | **定价器**：noul(会否发生)+score 与市价比价 | markets/polymarket.py |

## 3. 接线点（真机 flygate 上改这几处）

1. **现货方向/门**：`trader/engine.py:737`（`decision = tel["decision"]`）之后、
   `:755`（`if decision in (BUY, SELL)`）之前插入：

```python
if self.cfg.brain.jev_enabled:
    state = build_state(tel, holding=holding_dir_name, unrealized_pct=upnl_pct)
    violations = validate_no_identifiers(state)
    if violations:                      # 防前视：带标识的 state 拒绝外发
        self.log.warn("JEV", "deident_violation", detail=violations)
    else:
        ans = self.jev.ask_or_none(state, default_questions())
        if ans is None:                 # 降级：回落 tilt 决策，绝不阻塞交易循环
            tel["jev"] = {"error": "unavailable"}
        else:
            tel["jev"] = ans
            decision = jev_veto(decision, ans)   # 门：只砍单不改向（W2 再启用改向）
```

2. **合约腿**：`FuturesRunner._step` 同样位置同样代码（共享 `self.jev` 客户端）。

3. **教学归因**：`engine._close_trip` 里，多巴胺之前先问
   `attributable: noul`（这笔亏损是否该怪入场图式）与
   `pattern_class: choice`，只有 `attributable ≥ 阈值` 且类目 ∈
   {reversal_failure, regime_flip} 才发 punish 脉冲
   （替代手调 `punish_deadband_mult`）。

4. **回测臂**：`backtest.py` MODES 增加
   `jev_readout`（Jev 方向）、`jev_gate`（只否决）、`tilt_only`（显式 15 行阈值对照）、
   `jev_local`（base_url 指向 lichen/adapter，零成本对照）。
   `eval/protocol` 原样跑 DSR/PBO/MinTRL。

5. **配置**（`config.py` BrainConfig，按 flygate 现有 flag 风格带实测注释）：

```python
jev_enabled: bool = False       # 总开关；False 时零调用零成本
jev_model: str = "jev-1.13.0"   # 钉死版本 id，禁用 jev-latest（别名随发版漂移）
jev_cache_path: str = "runs/jev_cache.jsonl"   # 答案缓存：回测可复现、重跑免费
jev_base_url: str = "https://api.typesafe.ai"  # 可指向 lichen/adapter 做本地对照
jev_timeout_s: float = 8.0
jev_min_prob: float = 0.55      # 方向概率低于此值 → 按 HOLD 处理（门）
jev_attribution: bool = False   # 教学归因（P3 后启用）
```

## 4. 硬约束（写进代码与测试）

| 约束 | 实现位置 | 测试 |
|---|---|---|
| 版本钉死 | `jev.py DEFAULT_MODEL`，禁用别名 | 缓存 key 含 model |
| 确定性回放 | 答案缓存 jsonl，key=sha256(model+state+questions) | 同输入两次 → transport 只调 1 次 |
| 防前视 | `state.py` 白名单制 + `validate_no_identifiers`（拒日期/币名/绝对价） | 带 `2026-10-08`/`BTC_USDT` 的 state 必须被拦 |
| 超时降级 | `ask_or_none`，重试只对 429/5xx/网络错 | 持续失败 → 返回 None，不抛进引擎 |
| 英文提问 | questions 模板全英文常量 | 模板内无 CJK |
| 不用 confidence 做阈值 | 模板只取 choice 概率/score；confidence 仅记录 | — |
| 成本上限 | 只在果蝇 DN 触发（`tel.get("block")` 为空）时调用；Polymarket 每事件一次 | — |

## 5. 落地顺序与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| W1 | 离线判决：200 根 K 线 × Jev（本包 `decide/` 直接可用） | 输出"与 tilt 一致率"+"概率与后续涨跌相关性"两个数 |
| W2 | `jev_gate` 臂进回测 + paper 引擎（只在 DN 触发时调用） | ≥4 周 shadow 配对曲线；CF 账本出现 jev_veto 归因 |
| W3 | 教学归因（`jev_attribution`）+ Polymarket 腿事件采集 | 归因后 punish:reward 比例回正；Polymarket 校准曲线（Brier） |

## 6. 许可与署名

- Jev：TypeSafe 商业 API，价格/限流以 `docs.typesafe.ai` 为准（输入计费、输出免费）。
- 本地零成本对照：`Mushroom-Systems/lichen`（MIT，需 24GB NVIDIA）或
  `typesafe-ai/system-one-adapter-python`（MIT，通用 LLM 顶替同一接口）。
- MaleCNS 数据遵循 Janelia 上游许可；借用 StonkFly（MIT）部分需保留署名（真接线方案.md 已记）。
- 本扩展包随 flygate 主许可（MIT）分发。

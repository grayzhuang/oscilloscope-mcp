# scope-bench skill 实施计划

> 状态：**P0 + P1 + P2 已完成（2026-09-18）**——P0：skill 三份文案 +
> scope_cli(doctor/dump) + analyze 五子命令；P1：analyze 增 fft/hist/
> envelope、plot.py（wave/trend，降级 HTML）、scope_cli 增 meas-log/
> screenshot；P2：vcd2ref.py（最小 VCD 解析 + CSV 输入）+ compare_rtl.py
> （多形态输入 → reference_diff → markdown 报告）。36 个新增单测、全量
> 651 通过；ZDS1104 真机验证 P0/P1 全项 + P2 工具链闭环（真机 dump 为
> hw、合成理想 1 kHz VCD 为 ref：27/32 边匹配、Δ≤52 ns、窗口边缘差异
> 如实报告）。matplotlib 以可选依赖组 `.[plot]` 声明。
> **P2.3 的"真实 RTL 仿真参考"仍待 DUT**（当前闭环用合成 VCD 验证工具
> 链，README 该遗留未闭）。P3（README 三语 + 迁移个人侧）未开始。
> 放置决策：开发期随仓库 `skills/scope-bench/`；发布后迁移至个人 skill 目录并经
> skill-lock 登记来源。
> 执行入口：对 agent 说"执行 P0"即从任务 0.1 开始，按编号推进。

## 1. 背景与目标

oscilloscope-mcp 已提供 12 个 MCP 工具（仪器原语层，带 caveats 校验），但 agent
还缺两样东西：

1. **流程知识层**——什么目标 → 调哪个工具、什么顺序、参数怎么选、机型差异怎么
   应对、出了问题怎么排错；
2. **MCP 干不了的能力**——原始电压数组落盘（受 ~10 KB 响应预算限制）、离线
   分析、批量轮询、无 MCP 宿主时的直连操作。

本计划交付一个 skill（`scope-bench`）+ 5 个配套脚本补齐这一层。

**明确不做**：不新增 MCP 工具、不改 `server.py`；不覆盖 XY/ROLL 模式分析、示波器
端 MATH 通道、非 EDGE 触发参数细调（未真机验证区）、需信号源配合的频响扫描。

## 2. 已核实的事实基础（2026-09-18，实施时无需重复探索）

- **工具面**：`server.py` 注册 12 个工具：`scope_query` / `scope_screenshot` /
  `scope_trigger` / `scope_measure` / `scope_measure_stat` / `scope_channel` /
  `scope_timebase` / `scope_waveform` / `scope_acquire` / `scope_capture` /
  `scope_compare` / `scope_viewer`。
- **待桥接的分析函数**：`helpers/` 下 7 个已实现且有单测、但无 MCP 工具入口：
  `glitch_list`、`edge_interval_stats`、`pattern_search`、`voltage_histogram`、
  `fft_peaks`、`causality_check`、`envelope_downsample`。
- **依赖约束**：`fft_peaks` 为纯 Python radix-2 FFT，无需 numpy；仓库运行时依赖
  仅 `mcp` + `pyyaml`，脚本链路保持零第三方依赖（`plot.py` 的 matplotlib 为可选
  依赖，缺失时降级）。
- **产物约定**：`data/README.md`——`instruments.yaml`、`<model>_setup_*.yaml`、
  `<model>_waveform_*.csv`；目录内容一律不入库（`.gitignore` 已保证）。
- **测试基建**：驱动层已有 mock 传输单测模式（`test_rigol_ds1000z_driver.py`、
  `test_zds1000_driver.py`），在线脚本的离线测试沿用该模式。

## 3. 架构分工：三层

| 层 | 职责 | 状态 |
|---|---|---|
| MCP 工具 | 仪器原语（设置/捕获/测量/截图/viewer/对比），带 caveats 校验 | 已完备 |
| **skill（SKILL.md）** | 流程知识：目标 → 工具链、参数规则、机型坑、排错 | 本计划交付 |
| **脚本** | 原始电压落盘、离线分析、批量轮询、无宿主直连 | 本计划交付 |

脚本一律调库（`open_scope` + `Scope.set_*`），与 MCP 走同一条校验路径，
**不自行拼 SCPI**——这保证脚本与工具行为一致，且自动继承机型适配。

## 4. 交付物

```
skills/scope-bench/
├── SKILL.md               # 主文件 ≤120 行，只写工具契约层规则
├── references/
│   ├── recipes.md         # 按场景的完整调用序列（5–8 个配方）
│   └── models.md          # 机型差异 + 排错手册，按机型分节、只增不改
└── scripts/
    ├── scope_cli.py       # 在线：doctor / dump / meas-log / screenshot
    ├── analyze.py         # 离线：glitch / jitter / pattern / causality / bus
    │                      #       fft / hist / envelope
    ├── plot.py            # 离线：PNG 波形图 / 趋势图（HTML 降级）
    ├── vcd2ref.py         # 离线：VCD|CSV → reference edges JSON
    └── compare_rtl.py     # 离线：ref + hw → diff → markdown 报告
```

开发期调用方式：`conda run -n oscScope-mcp python skills/scope-bench/scripts/<x>.py`
（包在专用 env 以 `-e` 安装，脚本位置无关，为发布后迁移留好口子）。

## 5. 通用性设计规则（机型无关的保证）

1. **SKILL.md 主体不出现机型名**——规则一律引用工具响应字段（`status` /
   `caveats` / `image_format`），机型差异全部收进 `references/models.md`。
2. **脚本不硬编码**通道数、端口、模型名——从 `SCOPE_MCP_*` 环境变量或 profile
   能力读；参数校验复用库路径，不自己写判断。
3. **差异优先在 server 端暴露**：会影响工作流的差异应出现在工具响应的字段或
   caveats 里，models.md 只记响应暂时表达不了的少数事实。

新机型出现时的边界：

- **已支持家族的新型号**（DS1054Z、ZDS1000 系其他成员）：skill 与脚本零改动。
- **新厂商**：先在 server 侧加 profile + 驱动（插件架构本职）；skill 侧通常只需
  models.md 加一节，仅当新语义超出现有工具契约才动 skill 主体。
- **无驱动的示波器**：skill 无能为力，退到 `scope_query` 裸 SCPI 直通（无校验、
  无 caveats），skill 中写明此硬边界。

## 6. 阶段任务

### P0 — 核心闭环（覆盖约 80% 日常操作）

内部顺序：0.1 → 0.2 → 0.3 → 0.4（文案最后写，脚本接口冻结后）→ 0.5。

| # | 任务 | 验收标准（DoD） |
|---|---|---|
| 0.1 | `scope_cli.py`：`doctor` | read-only：env 检查 → IDN → profile 能力摘要 → 通道/时基/采集/触发快照；`--save` 落 `data/<model>_setup_<ts>.yaml`；mock 传输层单测通过 |
| 0.2 | `scope_cli.py`：`dump` | 电压 → `data/<model>_waveform_<ts>_<ch>.csv`（列 `t_s,volts`）+ 同名 `.meta.json`（t0/dt/n/scale/offset/caveats）；RAW 时 STOP 校验由驱动强制、脚本透传报错；多通道支持 |
| 0.3 | `analyze.py`：`glitch` / `jitter` / `pattern` / `causality` / `bus` | 输入为 MCP capture 结果 JSON 或 0.2 的 dump（宽松解析，认 `runs`/`edges` 键，顶层记 `schema` 版本）；输出 JSON 到 stdout，`--md` 出 markdown；每子命令 fixture 单测 |
| 0.4 | `SKILL.md` + `references/recipes.md` + `references/models.md`；仓库 `AGENTS.md` 架构节补一行 `skills/scope-bench/` 说明 | 对照 README 工具表人工 review；含触发词、授权前置、参数规则（threshold/hysteresis、NORMal vs RAW 语义、depth 档位、400 runs 截断 → 统计类分析走 dump 路径） |
| 0.5 | P0 整体验证 | `conda run -n oscScope-mcp python -m pytest tests/ -v` 全绿；doctor/dump 真机各跑一次（**需授权**，机器通电联网即可、无需信号）；新会话说"抓个波形"能激活 skill |

### P1 — 分析与可视化补全

| # | 任务 | 验收标准 |
|---|---|---|
| 1.1 | `analyze.py` 增加 `fft` / `hist` / `envelope` | 输入 dump 的 CSV + meta；纯 Python（复用 helper）；单测 |
| 1.2 | `plot.py` | 波形 PNG（多通道、触发点标记）、meas-log 趋势 PNG；matplotlib 可选，import 失败降级为复用 `helpers/viewer.py` 出 HTML——仓库核心保持零依赖 |
| 1.3 | `scope_cli.py` 增加 `meas-log` / `screenshot` | `--items --interval --duration` 轮询 → CSV + 末尾统计；截图按响应 `image_format` 定扩展名，时间戳命名；真机烟测（可选，需授权） |

### P2 — RTL 对比闭环

| # | 任务 | 验收标准 |
|---|---|---|
| 2.1 | `vcd2ref.py` | 最小纯 Python VCD 解析（单信号 + 层次名过滤，不引 pyvcd）+ CSV 输入；输出 `scope_compare` 直接可吃的 edges JSON；VCD fixture 单测 |
| 2.2 | `compare_rtl.py` | ref JSON + hw（capture JSON 或 dump 量化）→ `reference_diff` → markdown 报告（matched/shifted/missing/added/first_divergence + caveats） |
| 2.3 | 真机 + 真实仿真联跑 | 闭掉 README"scope_compare 未对真实 RTL 仿真验证过"遗留；需有数字信号的 DUT（Pi 4 demo 板即可）+ 对应仿真参考；**需授权** |

### P3 — 发布期（P2 验证通过后）

1. README 三语（`README.md` / `README.zh.md` / `README.ja.md`）各加一节 skill
   说明；工具面未变，"Tools exposed"表不动。
2. 迁移个人侧：skill 目录搬至个人 skill 位置并经 skill-lock 登记来源；
   `SKILL.md` 内脚本示例路径同步修订；脚本零改动（依赖 env 里的包而非相对路径）。

## 7. 验证策略

- **离线脚本**（analyze / plot / vcd2ref / compare_rtl）：无硬件 pytest，
  fixture 进出。
- **在线脚本**（scope_cli 各子命令）：mock 传输层离线单测；真机按 conformance
  惯例仅在用户授权时运行。
- **skill 文案**：对照 README 工具表 review + 新会话触发词实测。

## 8. 风险与预案

| 风险 | 预案 |
|---|---|
| capture JSON schema 随版本演进 | analyze.py 宽松解析 + 顶层 `schema` 版本字段，server 变更只改一处 |
| MCP 波形流 400 runs 截断影响 jitter/glitch 统计 | skill 明确：事件统计类分析走 RAW dump 路径，不走 MCP 波形流 |
| ZDS/RIGOL 语义差（SCREen=全内存、BMP、0.24–0.42 s 延迟） | 已在驱动/caveats 层处理，models.md 仅备案，脚本不特判 |
| 文档与脚本接口漂移 | 0.4 强制在 0.1–0.3 之后执行；接口冻结后再写文案 |

## 9. 工作量与真机需求

**开发全程不需要真机**；所有代码先离线完成并测试，真机只出现在 3 个验证点
（合计约 1 小时，可攒到机器方便时集中做）：

1. **P0.5（必做，约 10 分钟）**：doctor / dump 真机各跑一次；通电联网即可，
   无需被测信号。
2. **P1.3（可选，约 10 分钟）**：meas-log / screenshot 烟测，有信号时截图更有
   内容。
3. **P2.3（必做，约 30–60 分钟含排错）**：真实 RTL 仿真 vs 真机捕获；唯一时间
   不受开发侧控制的点（取决于 DUT 与信号）。

工作量（agent 连续工作时长，可按阶段拆）：

| 阶段 | 工作量 | 备注 |
|---|---|---|
| P0 | 5–9 小时（≈1 个工作日） | 大头：analyze 五个子命令接口与 fixture、三份文案 |
| P1 | 3–5 小时（≈半天） | fft/hist/envelope 是现成 helper 包装 |
| P2 | 4–7 小时（≈1 个工作日） | VCD 最小解析器占一半以上 |
| P3 | 1–2 小时 | README 三语 + 迁移 |

## 10. 待定项

- P0.5 / P1.3 / P2.3 真机验证的时机与目标机器（届时授权，不阻塞离线任务）。
- P2.3 的 DUT 与仿真参考来源。

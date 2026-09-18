# AGENTS.md — oscilloscope-mcp

MCP server（FastMCP，stdio，本地信任模型），让 AI agent 通过 SCPI/LAN 驱动真实台式示波器；核心场景是 `scope_compare` 做 RTL 仿真 vs 硬件实测的边沿 diff。Python ≥ 3.10，运行时依赖仅 `mcp` + `pyyaml`。开发分支 `dev`，PR 目标 `main`。

## 常用命令

本机运行环境是专用 conda env **`oscScope-mcp`**（`D:/conda/envs/oscScope-mcp`，Python 3.12，2026-09 建）。开发/测试/server 一律走它；MCP 宿主注册的 `command` 指向该环境 `python.exe` 的绝对路径（GUI 启动的宿主 PATH 与 shell 不同）。环境依赖的完整说明（分层、mcp<2 原因、Windows/conda 实测坑）见 `docs/environment.md`。

```bash
conda run -n oscScope-mcp python -m pytest tests/ -v   # 单元测试，无硬件；pyproject 设了 pythonpath=["src"]，免安装
conda run -n oscScope-mcp python -m pip install -e .   # 安装/重装（console script: oscilloscope-mcp）
conda run -n oscScope-mcp python -m oscilloscope_mcp   # 启动 server（需 SCOPE_MCP_HOST）

# 真机 conformance（会实际配置示波器，仅在用户授权下运行）
SCOPE_MCP_HOST=<ip> SCOPE_MCP_CONFORMANCE_MODEL=RIGOL_DS1104Z \
  conda run -n oscScope-mcp python -m pytest tests/test_instrument_conformance.py -v
# ZDS1104 同理（端口不设也行：profile 默认 5025 + 自动探测）
SCOPE_MCP_HOST=<ip> SCOPE_MCP_CONFORMANCE_MODEL=ZLG_ZDS1104 \
  conda run -n oscScope-mcp python -m pytest tests/test_instrument_conformance.py -v
```

仓库无 lint / typecheck 配置，风格跟随现有代码：`from __future__ import annotations`、包名 snake_case（`oscilloscope_mcp`）、仓库名 kebab-case。

## 架构与分层

```
src/oscilloscope_mcp/
├── server.py            # FastMCP 工具注册 + dispatch；每工具输出必带 caveats 字段
├── transport/scpi_lan.py    # 裸 TCP SCPI client，零第三方依赖
├── instruments/
│   ├── _base.py         # Scope ABC（厂商无关接口）+ 各 Setup/Result 数据类
│   ├── __init__.py      # MODEL_REGISTRY + open_scope() 分发
│   ├── rigol_ds1000z.py # RIGOL DS1000Z 驱动
│   ├── zlg_zds1000.py   # ZLG ZDS1000 驱动（ZDS1104 已过机）
│   └── profiles/*.yaml  # 能力声明（带宽、采样率、内存深度、idn_match 正则）
└── helpers/             # 纯分析函数（quantize/rle/edges/bus/fft_peaks/
                         #   reference_diff/caveat_calc…），不碰 I/O，可无硬件单测
```

`skills/scope-bench/` — agent 流程知识层（SKILL.md + recipes/models 参考 +
scope_cli/analyze 等脚本）：脚本一律调库走 MCP 同一校验路径，不自行拼
SCPI；机型差异只收在 `references/models.md`，SKILL.md 主体与脚本不出现
机型名；单测在 `tests/test_scope_cli.py`、`tests/test_analyze.py`（mock
传输层，无硬件）。实施计划见 `docs/scope-bench-plan.md`。

编辑规则：

- **能力是数据不是代码**：仪器支持什么由 profile YAML 声明；参数在发到硬件前校验，非法设置直接拒绝并给出合法值列表。
- profile 可用 `includes:` 深合并家族 fragment（`_ds1000z_family.yaml`），模型自身的键覆盖家族默认。
- 所有结构化输出必须带 `caveats[]`，观测限制（BW limit、内存降档、量化损失…）由 `helpers/caveat_calc.py` 集中计算并体现于此；agent 必须先读 caveats 再信数字。
- 新增仪器 = profile YAML + `Scope` 子类 + `MODEL_REGISTRY` 一行，不改核心模块；完成后跑真机 conformance。
- 环境变量前缀统一 `SCOPE_MCP_*`（`HOST` 必需；`PORT` 缺省时用选中 profile 的 `transport.default_port`（RIGOL 5555 / ZLG 5025），自动探测场景会先试 5555 再 5025 找应答 `*IDN?` 的端口；`MODEL` 缺省时靠 `*IDN?` 匹配 `idn_match`，要求恰好命中一个）。

## 硬件与验证边界

- **DS1104Z** 与 **ZLG ZDS1104**（fw 1.2.67，2026-09 过机）均已通过真机 conformance；DS1054Z 走同一代码路径但未上机验证，ZDS1000 家族其他型号同理。
- 非 EDGE 触发类型的具体参数、非 `NORMal` 采集类型：按官方编程手册实现并单测过，**未真机确认**——改动这些区域时不要臆造参数值。
- 连真机的测试（conformance、任何 live 工具调用）会实际改示波器状态，须在用户授权范围内运行。
- RAW waveform 模式要求示波器先 STOP（ZDS 的 `MULTiwave? MEMOry` 在 RUN 态返回 0 字节，驱动已强制校验）。RIGOL 的 NORMal 只是屏幕抽取（~1200 点）；**ZDS 的 `SCREen` 直接返回全量内存**（= MDEP 点数，RUN 态可读），两者语义不同，caveat 措辞注意区分。
- ZDS1104 已定标事实（勿再臆测）：WFM 流为 424/120 字节自然对齐小端；采样无符号 8-bit、码 128=屏幕中心、25 码/格、屏幕参照（正电压码值更小），`volts = (128−code)/25×div + offset`；采样率上限 1 GSa/s，1/2/4 通道降为 1G/500M/250M Sa/s；`:GLOBal:RUN:STATe?` 响应延迟 0.24–0.42 s（run_control 用轮询等待）；ASCII 响应以 `\r\n` 结尾；截图仅 BMP；`FORCE` 无 SCPI 等价（`:KEY 133` 未验证，故未注册）。
- `scope_query` 是无校验的 SCPI 直通，不做能力检查。

## 已知坑

- 源码 docstring 里残留 `bench/README.md`、`CLAUDE.md §利用想定` 的引用，是项目改名前的遗留，这两个文件已不存在。
- `docs/references/`（未入库）存放厂商编程手册（如 ZLG ZDS1000），为后续新增驱动准备的参考资料，勿删除。
- README 有三个语言版本（`README.md` 英 / `README.zh.md` 中 / `README.ja.md` 日），是公开项目门面；改工具签名或行为时三个版本同步改（含 “Tools exposed” 表）。

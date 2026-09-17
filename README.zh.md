[English](README.md) | [日本語](README.ja.md) | **简体中文**

# oscilloscope-mcp

一个让 AI agent（Claude Code、Cursor、自研 agent）通过网络操作真实台式示波器的
MCP 服务器。

两种用法：

**1. 交互式：你提要求，agent 操作示波器。**
不用伸手去工作台上拧旋钮、翻菜单，直接用自然语言说想要什么（*“在 CH1 的第 2
个下降沿上触发”*、*“给我看 CH1–CH3”*、*“频率是多少？”*），agent 就会在仪器上完成
配置、读回结果，并展示可交互的波形视图。

**2. AI agent 干活时的反馈通道。**
把 FPGA 开发交给 AI agent 时，它给 agent 提供设计在真实硬件上的行为反馈：agent
采集真实信号，自动核对是否与 RTL 仿真的预期一致，从此不需要有人肉眼看示波器来弥合
*“RTL 仿真 PASS / 上板 FAIL”* 的鸿沟。它也可以作为通用的输入通路：把各台仪器的
状态喂给 AI，让它根据看到的内容行动。

无论哪种方式，结果都以适合当前任务的形式给出：**agent 可以直接作为下一步输入的
结构化数据**（带 caveats 的测量值、边沿/电平段流）、供人快速查看的 **PNG 截图**，
或在浏览器里绘制实测样本的**单文件 HTML 查看器**。

*示波器在底层通过其标准 SCPI 接口控制，你不用写 SCPI，甚至不需要知道它的存在。*

![4 通道采集演示：agent 驱动配置 + RIGOL DS1104Z 截图](docs/images/hero-4ch-stacked-demo.png)

*一次 MCP 驱动的采集：AI agent 在 RIGOL DS1104Z 上设置了时基、V/div、各通道
偏移、通道标签和触发，然后把截图拉回给用户。四条迹线是 Raspberry Pi 4 上经
`pigpio` DMA 波形输出驱动的 GPIO 26 / 19 / 13 / 17，频率 50 / 100 / 200 / 500 Hz，
示波器逐一精确读回，端到端验证了整个闭环。所有设置都由 agent 通过本 MCP 服务器的
工具完成，没有一只手碰过前面板。*

## 特色

- **把仪器的全部能力以数据形式交给 agent，而不是封装几条命令的薄壳。** 完整能力集
  （例如 DS1000Z 的*全部 15 种*触发类型和约 95 个参数）声明在机器可读的 profile
  里，取自官方编程手册并在真机上验证过。agent 可以问*“这里能设什么？”*，拿回它
  实际可设的参数列表，而不局限于工具作者碰巧想到的那几个操作。

- **自然语言进，校验过的设置出。** 每个请求都会对照仪器实际支持的能力做检查；
  不可能生效的设置在*到达硬件之前*就被拒绝。坏请求会响亮地失败，而不是把示波器
  留在一个莫名其妙的状态。

- **每个操作同时也是可复现的类型化命令。** 你交互式提出的同样要求，也可以写成
  脚本、分享、跑在 CI 里。

- **结构化的事实 + 可人工核查的截图。** agent 提取数字和 caveats，同时返回 PNG
  截图供复核（可选光标/通道标签标注）。绝不做黑箱。

- **单文件 HTML 查看器。** 一个文件、零依赖、任何浏览器可打开。它读取示波器的
  RAW 实测样本并在浏览器里绘制，最大到示波器的全内存。操作方式与示波器本体一致：
  每通道 V/div、拖动 GND 偏移、吸附到采样点的光标、通道开关。

- **仿真 vs 硬件 diff。** 给它一列参考边沿（例如来自你的 RTL 仿真），它会与示波器
  实测的边沿做对比，返回结构化的 *added / missing / shifted* 边沿结果。

- **插件化架构。** 新增一个厂商/型号只需要两个文件：一个 SCPI 方言驱动和一个
  能力 profile YAML。能力是声明出来的数据，不是代码。

- **经过测试。** 约 590 个单元测试，外加对真实硬件的 live conformance 测试。

> 驱动目前覆盖 **RIGOL DS1000Z 系列**（DS1054Z / DS1074Z / DS1104Z）与
> **ZLG ZDS1000 系列**（ZDS1104）。**DS1104Z** 与 **ZDS1104** 已在真机上验证；
> 其余 RIGOL 型号走同一代码路径和 profile，但尚未做过 conformance 测试。

## 安装

```bash
git clone https://github.com/masahiro-999/oscilloscope-mcp.git
cd oscilloscope-mcp
pip install -e .
```

## 作为 MCP 服务器运行

```bash
# 设置示波器的网络地址。
export SCOPE_MCP_HOST=<your-scope-ip>     # 例如 192.168.1.42

# 启动服务器（默认 stdio 传输）。
python -m oscilloscope_mcp

# 或使用 console script。
oscilloscope-mcp
```

## 注册到 Claude Code

```bash
claude mcp add oscilloscope -- python -m oscilloscope_mcp
```

之后，连接到该 MCP 服务器的任何会话都可以使用 `scope_query`、`scope_screenshot`、
`scope_trigger`、`scope_measure`、`scope_measure_stat`、`scope_channel`、
`scope_timebase`、`scope_waveform`、`scope_acquire`、`scope_capture`、
`scope_compare` 和 `scope_viewer` 工具。

## 暴露的工具

| MCP 工具 | 用途 |
|---|---|
| `scope_query` | 原始 SCPI 命令直通。返回响应，外加一条 `caveats[]` 警告：此路径不做观测限制的自动检查。 |
| `scope_screenshot` | 保存当前示波器屏幕的 PNG 供用户查看。可选放置手动光标（Δt / ΔV）和通道标签（DIR / STP / NXT / CLK），让图片一目了然。 |
| `scope_trigger` | 读取或配置触发；覆盖仪器支持的**每一种**触发类型。无参调用即**读取**当前状态（返回 `mode`、全局参数、该模式的 `params`，以及描述可设项的 `accepted_params`）。要**设置**，传入 `mode` 和/或全局 `sweep` / `coupling` / `holdoff_s`，类型专属参数全部放进 `params` 对象（例如 N 边沿触发传 `{"source":"CHAN1","slope":"NEG","nth_edge":2,"idle_s":2e-3}`）。`mode` 会对 profile 的完整类型列表校验（DS1000Z 为 6 种标准 + 9 种选装授权类型），`params` 的每个键值都会对照该模式的 schema 校验（枚举成员、数值范围、通道源）。不支持的类型/参数会被拒绝并给出合法值列表；选装授权类型会触发 `caveats[]` 警告。传 `action` 控制采集：`RUN` / `STOP` / `SINGLE`（武装单次）/ `FORCE`（强制触发；仅 NORMAL/SINGLE sweep）。`status`（TD/WAIT/RUN/AUTO/STOP）反映结果。 |
| `scope_measure` | 读取示波器侧的自动测量（`:MEAS:ITEM?`）：对某个 `source` 通道读取一组 `items`。单源项：`VMAX VMIN VPP VTOP VBASE VAMP VAVG VRMS OVERSHOOT PRESHOOT PERIOD FREQUENCY RTIME FTIME PWIDTH NWIDTH PDUTY NDUTY`；双源项（需要 `source2`）：`RDELAY FDELAY RPHASE FPHASE`。不可测量的值以 `null` 返回并附 caveat。 |
| `scope_measure_stat` | 读取测量**统计**（`:MEASure:STATistic:ITEM`）：对某通道的一个或多个测量项读取累计的 current/max/min/average/deviation/count，而非单次瞬时值。`stat_types` 默认全部六项（`CURRent MAXimum MINimum AVERages DEViation COUNt`），可传子集限制查询范围。`mode` 可选设置统计模式（`DIFFerence` / `EXTRemum`）。`reset=True` 在查询前清空累计数据。返回 `{source, statistics: [{item, current, max, min, avg, dev, count}], caveats}`。 |
| `scope_channel` | 读取或配置模拟通道的**垂直**设置。`channel`（1 起）必填。可传 `scale_v_per_div`、`offset_v`、`coupling`（AC/DC/GND）、`display`、`probe`、`bw_limit`（20M/OFF）、`invert`、`units` 的任意子集。对照 profile 校验；20 MHz 带宽限制会触发 caveat。 |
| `scope_timebase` | 读取或配置**水平**（时基）设置。可传 `s_per_div`、`offset_s`、`mode`（MAIN/XY/ROLL）的任意子集。对照 profile 校验；长时基会触发内存降档 caveat。 |
| `scope_waveform` | 采集单个通道，返回紧凑的**阈值量化事件流**，而非原始 ADC 样本。原始电压先经施密特触发比较器（`threshold_v` ± `hysteresis_v`/2）量化为 0/1 流，再行程编码为 `runs` `[(t_us, level, dur_us)]` 和 `edges` `[(t_us, channel, RISE/FALL)]`（时间单位 µs，触发点在 t=0）。滞回是强制项：噪声边沿上用单一阈值会碎成无用的微脉冲。`hysteresis_v` 超过信号峰峰值 20 % 时触发漏检 `caveats[]` 警告；超出预算的事件流会截断并附 caveat。参数：`source`（默认 `CHAN1`）、`mode`（`NORMal` 或 `RAW`；RAW 读取**完整采集内存**，最多 24M 点，而不是 ~1200 点的屏幕抽取，且示波器须先停止）、`threshold_v`（1.5）、`hysteresis_v`（0.1）。 |
| `scope_acquire` | 读取或配置**采集模式**：`type`（NORMal/AVERages/PEAK/HRESolution）、`averages`（2..1024，2 的幂）、`memory_depth`（AUTO 或数值，按当前使能通道数校验）。无参调用即**读取**（返回 type、averages、memory_depth、sample_rate）。选择 AVERages 会触发 `caveats[]` 警告：刷新率降低。 |
| `scope_capture` | **声明式单次采集**：一次调用完成配置、武装、等待、读取。组合 trigger/channel/timebase/acquire 设置，武装 `:SINGle`，轮询直到触发（或 `timeout_s` 超时），然后读取各通道波形（量化为 `runs`/`edges`/`bus_runs`）、示波器侧 `measurements`，以及可选截图。设 `sweep='AUTO'` 或 `'NORMAL'` 可跳过武装+轮询，直接读当前屏幕内存。设 `waveform_mode='RAW'` 在触发后读取完整采集内存（SINGLE 之后示波器本就已停止）。返回 `{triggered, channels, waveform, bus_runs, measurements, screenshot_path, caveats}`。 |
| `scope_compare` | **仿真 vs 硬件参考 diff**：本项目的核心目标。接收来自 RTL 仿真器的金标边沿/电平段列表，与硬件实测（实时采集或传入数据）对比。返回 `{matched, shifted[{ref_t_us, hw_t_us, delta_us, kind}], missing[{t_us, kind}], added[{t_us, kind}], first_divergence_us, summary, caveats}`。两种模式：*live*（未提供 `hw_runs`/`hw_edges` 时从示波器采集）或 *offline*（直接传已采集数据，无需连接示波器）。`tolerance_us`（默认 0.05 = 50 ns）设定两个同类边沿相距多近才算匹配。信号无关：SPI、I2C、UART、ULPI 或任何数字信号都适用。 |
| `scope_viewer` | 生成**单文件可交互 HTML** 波形查看器。读取 RAW 波形数据（示波器须停止），把实测模拟电压样本嵌入一个 HTML 文件。功能：每通道 ON/OFF 开关、每通道 V/div 下拉（10mV–100V）、可拖动的 GND 偏移标记（▶）、滚轮缩放时按 1-2-5 步进的 T/div 下拉、拖拽平移、吸附到采样点并固定电压读数的垂直光标、触发位置标记（t=0 处的 ▼T，来自 `:TRIG:POS?`）。`depth` 控制以触发为中心的观测窗口：`"low"`（3 万点，~120µs，传输约 1s）、`"mid"`（30 万点，~1.2ms，约 3s）、`"high"`（300 万点以上，~12ms，约 17s）。深度越大传输越慢，因此通常选择触发附近的窗口，上限为示波器全内存。浏览器在拉远时按每像素 min/max 包络渲染数百万点，拉近时显示单个样本和圆点。 |

## 目录结构

```
oscilloscope-mcp/                 # 仓库（kebab-case）
├── README.md
├── LICENSE
├── pyproject.toml
├── src/oscilloscope_mcp/         # Python 包（snake_case）
│   ├── __init__.py
│   ├── __main__.py           # `python -m oscilloscope_mcp` 入口
│   ├── server.py             # FastMCP 服务器：工具注册 + 分发
│   ├── transport/
│   │   └── scpi_lan.py       # 裸 TCP SCPI 客户端（零第三方依赖）
│   ├── instruments/
│   │   ├── _base.py          # Scope ABC，厂商无关接口
│   │   ├── __init__.py       # MODEL_REGISTRY + open_scope() 分发
│   │   ├── rigol_ds1000z.py  # RIGOL DS1054Z / DS1104Z 驱动
│   │   ├── zlg_zds1000.py    # ZLG ZDS1000 系列驱动（ZDS1104）
│   │   └── profiles/
│   │       ├── _ds1000z_family.yaml  # 共享触发 + 采集 schema
│   │       ├── rigol_ds1104z.yaml
│   │       ├── rigol_ds1054z.yaml
│   │       ├── _zds1000_family.yaml  # ZDS1000 家族 schema
│   │       └── zlg_zds1104.yaml
│   └── helpers/
│       ├── caveat_calc.py    # 能力 + 当前设置 → caveats[]
│       ├── trigger.py        # 触发 profile：校验 / 归一化 / caveats
│       ├── measure.py        # 测量 profile：校验 / 归一化 / 解析
│       ├── acquisition.py    # 通道 + 时基 profile 校验
│       ├── quantize.py       # 原始电压 → 0/1 流（施密特滞回）
│       ├── rle.py            # 数字流 → runs[(t_us, level, dur_us)]
│       ├── edges.py          # runs → edges[(t_us, ch, RISE/FALL)]
│       ├── bus.py            # 多通道 runs → bus_runs[(t_us, value, dur_us)]
│       ├── reference_diff.py # 仿真 vs 硬件边沿 diff（项目的核心价值）
│       ├── viewer.py         # 单文件 HTML 波形查看器生成
│       ├── glitch_list.py    # 窄于 min_width 的电平段（runt/毛刺检测）
│       ├── edge_interval_stats.py  # 抖动 / 时钟稳定性统计
│       ├── pattern_search.py # RLE 位模式模板匹配
│       ├── voltage_histogram.py    # 电压分布 / 双峰检测
│       ├── fft_peaks.py      # top-N 频率峰值（EMI / 开关噪声）
│       ├── causality_check.py      # 跨通道 “B 在 N µs 内跟随 A” 检查
│       └── envelope_downsample.py  # 用于可视化的 min/max 抽取
└── tests/                    # 单元测试 + live conformance
```

## 环境变量

| 环境变量 | 必填 | 含义 |
|---|---|---|
| `SCOPE_MCP_HOST` | 是 | 示波器 IP 或主机名 |
| `SCOPE_MCP_PORT` | 否 | SCPI TCP 端口。未设置 → 用选中型号 profile 的 `default_port`（RIGOL 5555，ZLG 5025）；`*IDN?` 自动探测时会依次尝试常见端口（5555、5025） |
| `SCOPE_MCP_MODEL` | 否 | 型号键（如 `RIGOL_DS1104Z`）。未设置时解析 `*IDN?` 并与各 profile 的 `idn_match` 正则匹配 |

环境变量前缀 `SCOPE_MCP_*` 是项目专属的，不会与驱动其他工具的 shell 冲突。

## 新增一台仪器

两个新文件加一行注册：

1. **profile**（`src/oscilloscope_mcp/instruments/profiles/<model>.yaml`）：
   能力声明（模拟带宽、按通道数的采样率、内存深度、`idn_match` 正则）。
2. **驱动**（`src/oscilloscope_mcp/instruments/<vendor>_<series>.py`）：
   继承 `oscilloscope_mcp.instruments._base.Scope`，用该厂商的 SCPI 方言实现每个
   抽象方法。
3. **注册行**：在 `src/oscilloscope_mcp/instruments/__init__.py` 的
   `MODEL_REGISTRY` 中加一条。
4. **conformance 测试**：运行

   ```bash
   SCOPE_MCP_HOST=<ip> \
   SCOPE_MCP_CONFORMANCE_MODEL=<MODEL_KEY> \
   pytest tests/test_instrument_conformance.py -v
   ```

无需改动任何其他模块。

## 限制（工具警告）

所有结构化输出都带 `caveats[]` 字段。超出 profile 声明的限制（例如 DS1000Z 的
4 通道模式强制 250 MSa/s，实际可观测量降到约 25 MHz）会自动追加 caveat。agent
必须先读 `caveats[]` 再相信任何数字。

## 真机验证情况

以下内容已对实机 RIGOL **DS1104Z**（`tests/test_instrument_conformance.py`
套件）验证：连接 / IDN、原始 SCPI、截图、通道 / 时基 / 采集读写、测量与统计、
**NORMal 与 RAW（全内存）**波形采集、声明式 `scope_capture`、运行控制、全部
15 种触发类型的**模式切换**，以及各条校验（拒绝）路径。

同一套件也已对实机 ZLG **ZDS1104**（fw 1.2.67）通过：连接 / IDN / 端口自动探测、
原始 SCPI、BMP 截图、通道 / 时基 / 采集读写、测量与统计、**SCREen 与 MEMOry
（全内存，需先 STOP——由驱动强制）**波形采集、单次 `scope_capture`、运行控制、
全部 11 种触发类型的模式切换，以及各条拒绝路径。ZDS1000 的二进制 WFM 布局与
8 位采样编码（码 128 = 屏幕中心、25 码/格、屏幕参照）是通过 GND 耦合定标在该
实机上确定的。ZDS1000 专属注意：截图**仅 BMP**（请求 PNG 时回退为 BMP）、
`SINGLE` 是运行控制动作（不是 sweep 模式）、`FORCE` 动作尚无已注册的 SCPI
等价命令。

依据官方编程手册实现并通过单元测试，但**尚未在真机上确认**。在验证之前请按
“基于规格的实现”对待：

- **EDGE 之外的触发类型专属参数**（pulse width、slope time、RS232 / IIC / SPI
  的各字段…）。各类型的参数范围 / 枚举值来自手册并在发送前校验，**模式切换**
  已对全部类型过机，但非 EDGE 类型的单个参数未逐一在真机上通。
- **`NORMal` 之外的采集类型**（AVERages / PEAK / HRESolution）与显式
  `memory_depth` 值。
- **对真实仿真的 `scope_compare`。** diff 引擎有单元测试，并用实机采集边沿按已知
  偏移做过验证，但真正的 *RTL 仿真输出 vs 硬件采集* 全流程还没跑过。
- **DS1104Z 之外的型号**（DS1054Z / DS1074Z）与 **ZDS1104 之外的 ZDS1000
  型号**：同一代码路径 + profile，未做 conformance 测试。

## 安全 / 信任模型

这是一个**仅限本地的 stdio MCP 服务器**。它通过父进程的 stdin/stdout 与 MCP
客户端通信，不开放任何网络端口。客户端（Claude Code、Cursor、你自己的 agent）
以子进程方式启动它，信任边界就是本地用户账户。

在该边界之内：

- `scope_query` 是**无校验的原始 SCPI 直通**。客户端发送的任何 SCPI 都会被转发
  给示波器。这是有意设计：台式示波器的 SCPI 以读操作为主，即使改变设置（时基、
  通道档位、触发）也容易撤销。但这确实意味着启用了此 MCP 服务器的 agent 可以
  完全重配你的示波器并读取屏幕上的一切。请相应地使用。
- 与示波器本身的 SCPI 连接是示波器 LAN 口上的**无认证 TCP**（RIGOL DS1000Z 用
  5555，ZLG ZDS1000 用 5025）。同一网络上的任何设备本来就能与示波器通信。
  安装本 MCP 服务器不改变这一点，只是让 AI agent 拥有与你相同的访问权。

如果想进一步锁定示波器，请在示波器的网络层面（VLAN、防火墙）做，而不是在这一
层。MCP 服务器不是审查同机 agent 能对示波器做什么的合适位置。

## 测试

```bash
# 单元测试（无需硬件）。
pytest tests/ -v

# 对真实示波器的 live conformance。
SCOPE_MCP_HOST=<your-scope-ip> \
  pytest tests/test_instrument_conformance.py -v
```

## 为你的仪器添加支持

任何有**公开文档化控制接口**（SCPI 或其他）的示波器都可以支持。得益于上述插件
化架构，新增一个厂商 / 型号只是一个驱动子类加一个能力 profile，核心零改动。
见*新增一台仪器*一节。

**欢迎提交增加仪器支持的 fork。** 也欢迎提需求：为你想支持的仪器开一个 issue
（附上其编程手册链接更好），我们会考虑。

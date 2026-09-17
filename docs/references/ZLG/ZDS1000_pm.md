# ZDS1000 系列示波器程控指令速查

> **来源**：[ZDS1000_pm.pdf](./ZDS1000_pm.pdf)（广州致远仪器《ZDS1000 系列示波器编程手册》，UM01010101 V1.03，2024-04-19）的整理版，供 oscilloscope-mcp 新增 ZDS1000 驱动时查询。
> **页码约定**：本文 `p.NN` 指 PDF 页码；手册印刷页码（页脚/目录）= PDF 页码 − 6。
> **范围**：仅 SCPI 程控相关内容；硬件规格（带宽、最高采样率按型号差异）不在编程手册内，需另查规格书。
> **注意**：手册中大量示例以 ZDS2000/ZDS4054 系列为样机，ZDS1000 系列实际行为以真机为准；两通道机型不支持 `CHANnel3`/`CHANnel4` 参数（手册 §1.4，p.9）。

---

## 0. 驱动开发速览

### 0.1 连接参数（TCP）

| 项 | 值 | 出处 |
| --- | --- | --- |
| 传输 | 原始 TCP socket（非 VXI-11） | p.10 通用编程实例 |
| 端口 | **5025** | p.10、p.193（VISA 资源串 `TCPIP0::<ip>::5025::SOCKET`） |
| 命令编码 | ASCII，结束符 `\n`（或 `;`） | §1.2.3，p.8 |
| 默认 IP | `192.168.138.1`（手动模式） | `:SYSTem:LAN:IPADdress` 默认值，p.152 |
| 默认掩码/网关 | `255.255.255.0` / `192.168.138.255` | p.153–154 |
| 备选接口 | USB（RAW，VID 0x04CC / PID 0x121C，需 NI-VISA DriverWizard）、串口（115200-8-N-1） | §17，p.187–194 |

与现有 `scpi_lan.py`（默认 5555）的差异：**端口改 5025**（Siglent/Keysight 同款，transport 已支持自定义端口）。

### 0.2 采集与波形读取链路

```text
连接 TCP 5025 → *IDN? 识别 → :ACQuire/:CHANnel/:TIMebase/:TRIGger 配置
→ :RUN / :SINGle（可选 :STOP）→ :GLOBal:RUN:STATe? 查状态
→ :GLOBal:MULTiwave?<type>,<src1>[,<src2>...] 读波形
→ 响应 = 4 字节小端 int32 长度 + WFM 自描述二进制流（见 §10）
```

### 0.3 与 RIGOL DS1000Z（现有实现）的关键差异

| 项 | RIGOL DS1000Z | ZLG ZDS1000 |
| --- | --- | --- |
| TCP 端口 | 5555 | 5025 |
| 波形读取 | `WAV:DATA?` + `WAV:PRE?` 前置（yinc/yref 手工重建电压） | `:GLOBal:MULTiwave?` 一次返回自描述 WFM（刻度/采样率在头内） |
| 二进制响应 | IEEE `#` 块 | 截图走 IEEE `#9` 块；**波形走 4 字节小端长度前缀 + 裸流**（`query_binary` 现有解析不适用） |
| 屏幕/内存抽取 | `WAV:MODE NORMal/RAW`（RAW 需先 STOP） | `MULTiwave? SCREen/MEMOry`（是否需先 STOP 手册未写，见附录 A） |
| 运行状态查询 | `TRIG:STAT?` | `:GLOBal:RUN:STATe?` → `Run/Single/Stop` |

### 0.4 手册未覆盖项（摘要）

完整清单见[附录 A](#附录-a-手册未覆盖项与真机验证清单)。核心：采样点编码与码值→电压换算、WFM 头对齐/字节序、MEMOry 前置条件、ASCII 查询响应终止符、SCREen 点数。

---

## 1. SCPI 语法约定（§1，p.7–9）

- 命令以 `:` 开始，层级用 `:` 分隔；可用全名或大写缩写（大写字母不可省）。
- 命令与参数之间用一个空格；多参数用逗号分隔。命令中间不允许空格。
- 查询以 `?` 结尾；结束符建议 `\n`（`;` 亦可）。
- 参数类型：布尔型（`{0|OFF}`/`{1|ON}`，查询只返回 0/1）、离散型、整型（NR1）、实型（NR2/NR3）。
- 查询返回值多为 ASCII 科学计数法（如 `1.000000E+9`）。

## 2. IEEE 488.2 通用命令（§3，p.12–21）

| 命令 | 格式 | 功能 / 返回 | 页 |
| --- | --- | --- | --- |
| `*CLS` | `*CLS` | 清除所有状态数据结构、错误队列及 OPC 标记 | p.13 |
| `*ESE` | `*ESE <mask>` / `*ESE?` | 标准事件使能寄存器；mask 0–255，默认 0 | p.14 |
| `*ESR` | `*ESR?` | 标准事件状态寄存器，返回 0–255 | p.15 |
| `*IDN` | `*IDN?` | 设备信息，如 `ZHIYUANELECT,ZDS4054Plus,YJ201603240001_C2,V0.92,3.0.24.61125`（5 字段：厂商,型号,序列号,硬件版本,软件版本） | p.16 |
| `*OPC` | `*OPC` / `*OPC?` | 操作完成置位；查询恒返回 1 | p.17 |
| `*RST` | `*RST` | 恢复默认设置（通讯接口设置除外），等同 DefaultSetup | p.18 |
| `*SRE` | `*SRE <mask>` / `*SRE?` | 服务请求使能；0–255，默认 0 | p.19 |
| `*STB` | `*STB?` | 状态字节，0–255 | p.20 |
| `*TST` | `*TST?` | 自检，总返回 0 | p.21 |

## 3. ROOT 命令组（§4，p.22–30）

| 命令 | 功能 | 页 |
| --- | --- | --- |
| `:AUTosetup` | 自动定标 | p.23 |
| `:CLEar` | 一键清除 | p.24 |
| `:DEFault` | 默认设置（同 `*RST` 按键效果） | p.25 |
| `:PRINt` | 截屏（存仪器端） | p.26 |
| `:RUN` | 运行 | p.27 |
| `:SINGle` | 单次捕获 | p.28 |
| `:STOP` | 停止 | p.29 |
| `:TLHAlf` | 触发电平自动定位 50% | p.30 |

## 4. 捕获设置 ACQuire（§5，p.31–37）

| 命令 | 参数 / 范围 | 默认 | 返回 | 页 |
| --- | --- | --- | --- | --- |
| `:ACQuire:AVERages <count>` | 2–65536 整数，2 的整数次幂 | 64 | 整数 | p.32 |
| `:ACQuire:AUTOroll <bool>` | `{0\|OFF\|1\|ON}` | OFF | 0/1 | p.33 |
| `:ACQuire:MDEPth <mdep>` | `1400/14000/140000/700000/1400000/7000000/14000000/28000000/AUTO` | 1400 | 点数 | p.34 |
| `:ACQuire:MAREa <mode>` | `{AUTO\|FIXEd}` | AUTO | 文本 | p.35 |
| `:ACQuire:SRATe?` | — | — | 科学计数法，如 `1.000000E+9`（1 GSa/s） | p.36 |
| `:ACQuire:TYPE <type>` | `{NORMal\|PEAK\|AVERages\|HRESolution}` | NORMal | 文本 | p.37 |

## 5. 自校准 CALibrate（§6，p.38–42）

| 命令 | 功能 | 返回 | 页 |
| --- | --- | --- | --- |
| `:CALibrate:DATE?` | 最后自校准日期 | `<year>,<month>,<day>` | p.39 |
| `:CALibrate:TIME?` | 最后自校准时间 | `<hours>,<minutes>,<seconds>` | p.40 |
| `:CALibrate:STARt` | 启动自校准 | — | p.41 |
| `:CALibrate:QUIT` | 退出自校准 | — | p.42 |

## 6. 通道 CHANnel\<n\>（§7，p.43–53）

`<n>` ∈ `{1|2|3|4}`（两通道机型仅 1/2）。均可设置/查询。

| 命令 | 参数 / 范围 | 默认 | 返回 | 页 |
| --- | --- | --- | --- | --- |
| `:CHANnel<n>:DISPlay <bool>` | `{0\|OFF\|1\|ON}` | OFF | 0/1 | p.44 |
| `:CHANnel<n>:VERNier <bool>` | 垂直档位粗调/微调切换 | OFF | 0/1 | p.45 |
| `:CHANnel<n>:SCALe <value>` | 实型，2 mV/div–5 V/div（×探头衰减比后同乘） | — | 科学计数法 | p.46 |
| `:CHANnel<n>:OFFSet <value>` | 实型；2–100 mV/div 档为 ±2 V，200 mV–5 V/div 档为 ±40 V | — | 科学计数法 | p.47 |
| `:CHANnel<n>:COUPling <type>` | `{DC\|AC\|GND}` | DC | 文本 | p.48 |
| `:CHANnel<n>:BWLimit <type>` | `{OFF\|20M}`（20 MHz） | OFF | 文本 | p.49 |
| `:CHANnel<n>:UNITs <type>` | `{VOLTage\|AMPere}`（探头类型） | — | 文本 | p.50 |
| `:CHANnel<n>:PROBe <type>` | 电压探头 `0.1/0.2/0.5/1/2/5/10/20/50/100/200/500/1000`；电流探头 `10/5/2/1/0.5/0.2/0.1/0.05/0.02/0.01/0.005/0.002/0.001`（须按字面写入，如 `10` 而非 `10.0`） | — | 数值 | p.51 |
| `:CHANnel<n>:INVert <bool>` | 反相 | OFF | 0/1 | p.52 |
| `:CHANnel<n>:DELAy <value>` | 实型，−100–100 ns | — | 科学计数法 | p.53 |

> **手册不一致**：第 7 章命令列表（p.43）列有 `:CHANnel<n>:TERMination`，但正文无对应详情页，参数范围未知——接入前需真机验证。

## 7. 光标 CURSor（§8，p.54–66）

`<mode>` ∈ `{OFF|VERTical|HORIzontal|ALL}`，默认 OFF（p.55）。

**位置类**（设置/查询像素位置）：

| 命令 | 范围 | 说明 | 页 |
| --- | --- | --- | --- |
| `:CURSor:X1Position <pos>` / `X2Position` | 整型 0–699 | 水平像素位置；HORIzontal 模式下无效 | p.56–57 |
| `:CURSor:Y1Position <pos>` / `Y2Position` | 整型 0–399 | 垂直像素位置；VERTical 模式下无效 | p.62–63 |

**数值类**（时间/电压值）：

| 命令 | 说明 | 页 |
| --- | --- | --- |
| `:CURSor:X1Value <value>` / `X2Value` | 设置/查询光标水平位置对应的时间值（科学计数法） | p.58–59 |
| `:CURSor:XDELta?` | ΔX = X1 − X2 | p.60 |
| `:CURSor:IXDElta?` | 1/ΔX | p.61 |
| `:CURSor:Y1Value?<source>` / `Y2Value?<source>` | 查询指定通道光标电压值；`<source>` ∈ CH1–4 | p.64–65 |
| `:CURSor:YDELta?<source>` | ΔY | p.66 |

**电压/位置与时间/位置换算公式**（V1.03 新增，p.56、p.62；由版面坐标还原）：

$$x_{\mathrm{cursor}} = 350 + \frac{t_{\mathrm{set}} - t_{\mathrm{offset}}}{t_{\mathrm{div}}} \times 50$$

$$y_{\mathrm{cursor}} = 200 - \frac{u_{\mathrm{set}} - u_{\mathrm{offset}}}{u_{\mathrm{div}}} \times 50$$

其中 $t_{\mathrm{set}}$ 为需设置的时刻（s），$t_{\mathrm{offset}}$ 为水平偏移量（s），$t_{\mathrm{div}}$ 为水平档位（s/div）；$u_{\mathrm{set}}$、$u_{\mathrm{offset}}$、$u_{\mathrm{div}}$ 为对应电压量（V、V、V/div）。位置为屏幕像素：$x_{\mathrm{cursor}} \in [0,699]$，$y_{\mathrm{cursor}} \in [0,399]$；由系数 50 点/格与中心 350/200 可知波形区为 14 格 × 8 格。垂直方向 $y$ 轴向下为正，与电压方向相反，故取负号。

## 8. 显示 DISPlay（§9，p.67–75）

| 命令 | 参数 / 范围 | 默认 | 页 |
| --- | --- | --- | --- |
| `:DISPlay:VECTors <bool>` | 矢量（线）/点显示 | OFF | p.68 |
| `:DISPlay:PERSistence <time>` | `{OFF\|0.1\|0.2\|0.5\|1\|2\|5\|10\|20\|50\|INFinite}`（s） | OFF | p.69 |
| `:DISPlay:COLOrgraded <bool>` | 色温显示 | OFF | p.70 |
| `:DISPlay:WBRightness <value>` | `{0,10,…,100}`（%） | 50 | p.71 |
| `:DISPlay:GBRightness <value>` | `{0,10,…,100}`（%） | 50 | p.72 |
| `:DISPlay:FREEze <bool>` | 冻结显示 | OFF | p.73 |
| `:DISPlay:PCLEar` | 清除余辉 | — | p.74 |
| `:DISPlay:DATA?` | 读取屏幕位图 | — | p.75 |

**`:DISPlay:DATA?` 返回格式**（p.75）——标准 IEEE 块 + 尾部 `\n`，与现有 `query_binary` 兼容：

```text
文件头：'#9' + 9 位十进制长度（默认 #9001152054）
数据流：BMP 位图，800×480×3 + 54（BMP 头）= 1152054 字节
文件尾：'\n' (0x0A)
```

## 9. 波形数据 GLOBal（§10，p.76–79）★ 驱动核心

### 9.1 `:GLOBal:RUN:STATe?`（p.77）

返回 `Run`、`Single`、`Stop`。

### 9.2 `:GLOBal:MULTiwave?`（p.78–79）

```text
:GLOBal:MULTiwave?<type>,<source1>[,<source2>[,<source3>[,<source4>]]
```

| 参数 | 范围 | 说明 |
| --- | --- | --- |
| `<type>` | `{SCREen\|MEMOry}` | SCREen = 屏幕显示点；MEMOry = 全存储深度（对应 Rigol NORMal/RAW） |
| `<source1..4>` | `{CHANnel1\|CHANnel2\|CHANnel3\|CHANnel4}` | source2–4 可省略，最多一次读 4 通道 |

**返回格式：`4 字节长度 + WFM 文件流`**

1. **数据长度**：4 字节二进制，小端 int32（手册强调"是二进制数据，不是 ASCII"）。
2. **WFM 文件流**：二进制文件，布局 `Head + Item[n] + Data[n]`。

```c
struct wfm_head_info {          // 按 x86 自然对齐推算 sizeof = 424 B
    char   cFileType[4];        // 固定为 "WFM"
    char   cDevName[64];        // 设备名称
    char   cFirmwareVersion[128];
    char   cDataFormat[40];     // 数据格式版本，"Vx.xx"（采样编码规则未在手册中说明）
    int    iRev0;               // 保留
    double dfHrztDivision;      // 水平档位 [s]
    double dfHrztOffset;        // 水平偏移 [s]
    double dfStartTime;         // 数据起始时间 [s]
    int    iAcqMode;            // 采样模式：0 标准 / 1 峰值 / 2 平均 / 3 高分辨率
    int    iRev1;               // 保留
    double dfSampleRate;        // 采样率 [Hz]
    int    iTrigMode;           // 触发模式：0 自动 / 1 普通
    int    iTrigSource;         // 触发源：0 CH1 / 1 CH2 / 2 CH3 / 3 CH4（手册原文"2通道"疑漏"3"）
    char   cTrigType[64];       // 触发类型
    int    iItemNum;            // 数据项（通道）个数
    int    iRev[17];            // 保留
};

struct wfm_item_info {          // 按 x86 自然对齐推算 sizeof = 120 B
    int    iChannel;            // 通道
    int    iCoupleMode;         // 耦合：0 DC / 1 AC / 2 GND
    int    iBwLimit;            // 带宽限制：0 关闭 / 20 MHz
    int    iProbeType;          // 探头类型：0 电压 / 1 电流
    int    iReversed;           // 反相：0 关闭 / 1 反相
    int    iRev0;               // 保留
    double dfProbeAtt;          // 探头比率
    double dfVertDivision;      // 垂直档位 [V]
    double dfVertOffset;        // 垂直偏移 [V]
    int    iDataLength;         // 原始数据长度（点数）
    int    iDataOffset;         // 原始数据相对于文件的偏移 [B]
    int    iRev1[16];           // 保留
};
```

**解析注意**：

- 手册未声明结构体对齐/填充规则与成员字节序；上面两个 `sizeof` 为按 x86 自然对齐的推算值，**真机验证后才能写死偏移**（推荐用 `cFileType=="WFM"` + `iItemNum` 交叉校验）。
- `iDataOffset` 为"相对于文件的偏移"，即从 4 字节长度前缀后的 WFM 流起始处计算；多通道时各通道数据块按各 item 的 offset 定位（是否顺序排列/交织未说明）。
- Data[n] 的采样编码（位宽、有无符号、码值→电压映射）手册未给出——这是驱动电压重建与量化 caveat 的最大缺口，见附录 A。
- 参考例程：p.10–11"例程 3：读取波形"（先 `recv` 4 字节 int32 长度，再循环收满）。

## 10. 按键 KEY（§11，p.80–82）

```text
:KEY <keyval>,<bool>    // <bool> 为长按标记 {0|OFF|1|ON}；不支持查询
```

键值查找表（表 11.1）：

| 功能 | 键值 | 功能 | 键值 | 功能 | 键值 |
| --- | --- | --- | --- | --- | --- |
| SUB1–SUB6 | 16–21 | H_S←/→/↓ | 80/81/82 | CH3 / CH4 | 98 / 99 |
| MenuBack | 32 | H_O←/→/↓ | 83/84/85 | CH_S←/→/↓ | 100/101/102 |
| Touch | 33 | Acquire | 86 | CH_O←/→/↓ | 103/104/105 |
| A←/→/↓ | 34/35/36 | LA | 149 | T← / T→ | 128 / 129 |
| B←/→/↓ | 37/38/39 | Math / Decode / Ref | 144/145/146 | T↓ / Trigger | 130 / 131 |
| SELECT | 40 | Utility / Source | 147/148 | Auto/Normal / Force | 132 / 133 |
| Persist / Cursor / Intensity / Clear / PrintScreen / Measure / Seg / Zoom / Roll | 41–49 | Auto / Run/Stop / Single / Default | 64–67 | CH1 / CH2 | 96 / 97 |

示例：`:KEY55,0` 开启 ZOOM 视图（55 未在表中列出，Zoom=48；以真机为准）。

## 11. 数学运算 MATH（§12，p.83–89）

| 命令 | 参数 / 范围 | 页 |
| --- | --- | --- |
| `:MATH:MODE <mode>` | `{OFF\|BASIc\|FFT}`，默认 OFF | p.84 |
| `:MATH:BASIc:OPERator <oper>` | `{ADD\|SUBTract\|MULTiply\|DIVIsion\|DIFFerential\|INTEgral}` | p.85 |
| `:MATH:BASIc:SA/SB <source>` | CH1–4 | p.85 |
| `:MATH:BASIc:INVErt <bool>` | 反相 | p.85 |
| `:MATH:SCALe <value>` | 档位，参考用户手册，不支持微调档位 | p.86 |
| `:MATH:OFFSet <value>` | 偏移，精度为垂直档位的 1/50 | p.87 |
| `:MATH:FFT:SOURce <source>` | CH1–4 | p.88–89 |
| `:MATH:FFT:SINGle` / `:MATH:FFT:RST` | 单次运算 / 显示复位 | p.88 |
| `:MATH:FFT:VSMode <mode1>` | `{DBVRms\|VRMS\|AMPL\|PSD}` | p.88 |
| `:MATH:FFT:WINDow <mode2>` | `{RECTangle\|HANNing\|HAMMing\|BLACkman}` | p.88 |
| `:MATH:FFT:HSCAle <value1>` / `:MATH:FFT:HOFFset <value2>` | 水平档位/偏移，范围参考用户手册 | p.88 |

## 12. 测量 MEASure（§13，p.90–138）

**统一子命令模式**（以 `VPP` 为例，`<src>` ∈ `{CHANnel1..4|Math}`）：

```text
:MEASure:VPP <src>               使能
:MEASure:VPP? <src>              当前值（科学计数法）
:MEASure:VPP:STATe? <src>        状态：Valid / ? / Invalid（有效 / 不准确如超量程 / 无效）
:MEASure:VPP:CURRent? <src>      当前值
:MEASure:VPP:MAXImum? <src>      历史最大
:MEASure:VPP:MINImum? <src>      历史最小
:MEASure:VPP:AVERage? <src>      平均
:MEASure:VPP:DEViation? <src>    标准差
:MEASure:VPP:COUNt? <src>        计数
```

其他命令（`:MEASure:CLEar` 清除全部测量项并关闭测量，p.92；`:MEASure:THResholds <mode>,<value>` 设高/中/低阈值百分比，高 40–100、中 20–80、低 0–60，p.93）按各自参数展开同一模式。

**测量项分组**（标注 ★ 的不含 `:STATe?` 子命令）：

| 组 | 参数形式 | 项目 |
| --- | --- | --- |
| 电压/时间单源 | `<src>` | VPP(峰峰值)、VAMP(幅度)、VMAX、VMIN、VTOP(基顶)、VBASe(基底)、ROVErshoot/FOVErshoot(过冲)、RPREshoot/FPREshoot(预冲)、VMEAn(校准平均)、PERiod、FREQuency、RISetime、FALLtime、PWIDth、NWIDth、PDUTy、NDUTy、BWIDth、XMAX/XMIN(最大/最小电压时间值)、PULSetrain(脉冲串，另含 `:PSET <n>` 设脉冲串个数，结果以 16 进制返回"尚未做进一步处理") |
| 平均/面积（带区间） | `<interval>,<src>`，`<interval>` ∈ `{DISPlay\|CYCLe}`（全屏/N 周期） | VAVG；★AREA、PAREa、NAREa |
| 有效值 | `<couple>,<interval>,<src>`，`<couple>` ∈ `{DC\|AC}` | VRMS |
| 比率（双源） | `<interval>,<src1>,<src2>`（4 通道机 src1≠src2，2 通道机仅 src1） | VRATio |
| 通道间延迟/相位（双源） | `<src1>,<src2>` | RRDelay、FFDelay、RFDelay、FRDelay（A→B 边沿组合延迟）、RPHase、FPHase |
| 计数类 ★ | `<src>`（TCOUnt 不支持 Math） | BAUD(波特率)、RCOUnt/FCOUnt(上/下降沿计数)、PCOUnt/NCOUnt(正/负脉冲计数)、TCOUnt(触发计数) |
| 建立保持 ★ | `<src>` | SETUptime、HOLDtime、SHRAtio(建立保持比率) |

**建立保持配置**（`:MEASure:SHOLd`，p.126，仅设置/查询配置、无测量值子命令）：

```text
:MEASure:SHOLd:TCH <src>     时钟通道
:MEASure:SHOLd:DCH <src>     数据通道（手册参数表写 CH1–4，返回示例写 CH1/CH2，以真机为准）
:MEASure:SHOLd:SAMP <type>   采样类型 {POSitive|NEGative|EITHer}
```

## 13. 系统设置 SYSTem（§14，p.139–154）

| 命令 | 参数 / 返回 | 页 |
| --- | --- | --- |
| `:SYSTem:ERRor[:NEXT]?` | 查询并删除最新错误，如 `-113,"Undefined header"` | p.140 |
| `:SYSTem:ERRor:COUNt?` | 当前错误个数（整型） | p.141 |
| `:SYSTem:VERSion?` | SCPI 版本，返回 `1999.0` | p.142 |
| `:SYSTem:LANGuage <language>` | `{SCHinese\|ENGLish}` | p.143 |
| `:SYSTem:BEEPer <bool>` | 按键声 | p.144 |
| `:SYSTem:AOUTput <aux>` | 辅助输出 `{TOUT\|PFAil}` | p.145 |
| `:SYSTem:EXPand <mode>` | 垂直扩展 `{GROund\|CENTer}` | p.146 |
| `:SYSTem:DATE <year>,<month>,<day>` | 1900–2099 / 01–12 / 01–31；返回如 `2014,01,01` | p.147 |
| `:SYSTem:TIME <h>,<m>,<s>` | 0–23 / 0–59 / 0–59；返回如 `08,29,59` | p.148 |
| `:SYSTem:LAN:STATus?` | `Unlink` / `Linked` | p.149 |
| `:SYSTem:LAN:MAC?` | 十六进制，如 `005023341250` | p.150 |
| `:SYSTem:LAN:MODE <mode>` | `{DHCP\|MANUal}`，默认 MANUal | p.151 |
| `:SYSTem:LAN:IPADdress <a0>,<a1>,<a2>,<a3>` | a0: 0–223(非 127)，其余 0–255；默认 `192,168,138,1`；仅手动模式可设 | p.152 |
| `:SYSTem:LAN:SMASk <m0>...<m3>` | 默认 `255,255,255,0`；仅手动模式可设 | p.153 |
| `:SYSTem:LAN:GATEway <g0>...<g3>` | 默认 `192,168,138,255`；仅手动模式可设 | p.154 |

## 14. 水平时基 TIMebase（§15，p.155–160）

| 命令 | 参数 / 范围 | 默认 | 页 |
| --- | --- | --- | --- |
| `:TIMebase:MODE <type>` | `{MAIN\|XY\|ROLL}` | MAIN | p.156 |
| `:TIMebase:SCALe <value>` | 1/2/5/10/20/50/100/200/500 ns → us → ms → 1/2/5/10/20/50 s（1-2-5 序列） | — | p.157 |
| `:TIMebase:OFFSet <value>` | 实型 | — | p.158 |
| `:TIMebase:ZOOM:SCALe <value>` | 500 ps → … → 1000 s（1-2-5 序列） | — | p.159 |
| `:TIMebase:ZOOM:OFFSet <value>` | 实型 | — | p.160 |

示例：`:TIMebase:SCALe100ms` 或 `:TIMebase:SCALe0.1s` 等效（p.157）。

## 15. 触发 TRIGger（§16，p.161–185）

### 15.1 触发通用命令（p.162–166）

| 命令 | 参数 / 范围 | 默认 | 页 |
| --- | --- | --- | --- |
| `:TRIGger:SWEep <mode>` | `{AUTO\|NORMal}` | AUTO | p.162 |
| `:TRIGger:HOLDoff <value>` | 实型 0–34 s | 0 | p.163 |
| `:TRIGger:SENSitivity <value>` | 0.0–1.5（× 当前垂直档位，仅手动模式） | 0 | p.164 |
| `:TRIGger:COUPling <mode>` | `{DC\|AC\|LFReject\|HFReject}` | DC | p.165 |
| `:TRIGger:MODE <mode>` | `{EDGE\|PULSe\|SLOPe\|VIDEo\|RUNT\|PRUNt\|PATTern\|NEDGe\|DELay\|TIMeout\|SHOLd}` | AUTO | p.166 |

### 15.2 各触发类型专有命令

以下 `<level>` 类参数范围均为"屏幕中心 $\pm 5.12 \times$ 垂直档位"（与通道单位一致）。

| 类型 | 子命令 | 取值 / 范围 | 页 |
| --- | --- | --- | --- |
| EDGE | `:EDGE:SOURce` / `:SLOPe` / `:LEVel` | 源 `{CH1..4\|LINE\|EXTernal}`；边沿 `{POSitive\|NEGative\|EITHer}`；默认 CH1/POSitive/0（LINE 不支持设电平） | p.167 |
| PULSe | `:SOURce` / `:WHEN` / `:UWIDth` / `:LWIDth` / `:LEVel` | 源同 EDGE；脉宽类型 `{PGReater\|PLESs\|PGLess\|NGReater\|NLESs\|NGLess}`；上下限 1 ns–1 s | p.168 |
| SLOPe | `:SOURce` / `:WHEN` / `:TUPPer` / `:TLOWer` / `:WINDow` / `:HLEVel` / `:LLEVel` | 源 CH1–4；类型同 PULSe 的 WHEN；时间上下限 1 ns–1 s；窗口 `{TA\|TB\|TAB}`；电平需 levela > levelb | p.169–170 |
| VIDEo | `:SOURce` / `:POLArity` / `:STANdard` / `:SLOPe` / `:LINE` / `:LEVel` | 源 CH1–4；极性 `{POSitive\|NEGative}`；制式 `{NTSC\|PAL\|SECAM}`；模式 `{ANYLine\|GOTOline\|ANYFiled\|EVENfield\|ODDField}`（默认 ANYFiled）；行号 1–n 按制式 | p.171–172 |
| RUNT（欠幅） | `:SOURce` / `:SLOPe` / `:WHEN` / `:TUPPer` / `:TLOWer` / `:WINDow` / `:HLEVel` / `:LLEVel` | 源 CH1–4；边沿 `{POSitive\|NEGative\|EITHer}`；限定符 `{NONE\|GREater\|LESS\|INRange}`；时间上下限 2 ns–1 s；窗口/电平同 SLOPe | p.173–174 |
| PRUNt（超幅） | 同 RUNT | 时间上下限 8 ns–1 s，其余同 RUNT | p.175–176 |
| PATTern（码型） | `:ASRc` / `:BSRc` / `:APat` / `:BPat` / `:WHEN` / `:TUPPer` / `:TLOWer` / `:LEVel <source>,<level>` | 信源 CH1–4；码型 `{H\|L\|X\|R\|F}`（默认 H）；限定符 `{NONE\|GREater\|LESS\|INRange\|OUTRange}`；tlower 4 ns–1 s、tupper 5 ns–1 s（手册参数表名称列与范围列有错位，已按语义整理） | p.177–178 |
| NEDGe（N 边沿） | `:SOURce` / `:SLOPe` / `:EDGEnum` / `:IDLE` / `:LEVel` | 源 CH1–4；边沿 `{POSitive\|NEGative}`；边沿数 1–65535；空闲 10 ns–1 s | p.179 |
| DELay | `:ASRc` / `:BSRc` / `:SLOPe` / `:WHEN` / `:TUPPer` / `:TLOWer` / `:LEVel <source>,<level>` | 信源 CH1–4；模式 `{RTOR\|RTOF\|FTOR\|FTOF}`（A 边沿→B 边沿组合）；限定符 `{GREater\|LESS\|INRange}`；时间上下限 1 ns–1 s | p.181–182 |
| TIMeout | `:SOURce` / `:SLOPe` / `:TIMe` / `:LEVel` | 源 CH1–4；边沿 `{POSitive\|NEGative\|EITHer}`；超时 8 ns–4 s | p.183 |
| SHOLd（建立保持） | `:DSrc`(时钟) / `:CSrc`(数据) / `:SLOPe` / `:PATTern` / `:TYPE` / `:STIMe` / `:HTIMe` / `:LEVel <source>,<level>` | 源 CH1–4；采样类型 `{POSitive\|NEGative}`；数据类型 `{H\|L}`；触发类型 `{SETup\|HOLd}`；建立/保持时间 2 ns–1 s | p.184–185 |

## 16. VISA / USB / 串口（§17，p.186–194）

- NI-VISA 资源串：USB `USB?*`；TCP `TCPIP0::192.168.138.46::5025::SOCKET`；串口 `ASRL1::INSTR`（115200 波特、8 数据位、1 停止位、无校验）。
- USB 为 RAW 设备（非 USBTMC），VID=0x04CC、PID=0x121C，需 NI-VISA DriverWizard 装驱动。

---

## 附录 A. 手册未覆盖项与真机验证清单

接入 ZDS1000 驱动（profile YAML + `Scope` 子类 + conformance）前，以下数据手册未给，**不得臆造**，须真机或规格书确认：

1. **采样点编码与电压换算**：`cDataFormat` 仅为版本串；采样位宽（8 bit？）、有无符号、码值满量程对应格数、raw→V 公式均未说明。影响：电压重建、`caveat_calc` 量化损失。
2. **WFM 结构体对齐/字节序**：`sizeof(wfm_head_info)=424`、`sizeof(wfm_item_info)=120` 为自然对齐推算；double 成员字节序未明示（长度前缀已明确小端）。影响：二进制解析正确性。
3. **多通道 Data 布局**：`iItemNum>1` 时各通道数据块按 `iDataOffset` 排列还是交织，未说明。
4. **MEMOry 读取前置条件**：是否需先 `:STOP`（Rigol RAW 需要）；运行态读 SCREen/MEMOry 的行为；28 M 点满深度传输耗时（影响 socket 超时/`recv_max`）。
5. **ASCII 查询响应终止符**：手册只规定命令端 `\n`；响应是否也以 `\n` 结尾未写（现有 `_recv_until_newline` 依赖此）。
6. **SCREen 模式点数**：屏幕抽取返回多少点未写（影响 caveats 的抽取损失描述；由光标公式推波形区为 700×400 像素，但非采样点数）。
7. **`:CHANnel<n>:TERMination`**：列于目录/章首页但无详情页。
8. **硬件规格**（profile 需要）：各型号带宽、最高采样率、`idn_match` 所需 IDN 字符串格式（示例为 ZDS4054Plus，ZDS1000 实际 IDN 待采）。
9. **手册原文疑点**：`wfm_head_info.iTrigSource` 注释"2通道"疑漏"3"；`:MEASure:SHOLd` 通道返回示例只写 CH1/CH2；PATTern 参数表名称/范围错位；KEY 例程 `:KEY55,0` 的 55 未在键值表中（Zoom=48）。

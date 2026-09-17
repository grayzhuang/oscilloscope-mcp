# 环境与依赖说明

本文说明 oscilloscope-mcp 的依赖构成、版本约束的原因、推荐的环境形态，以及在 Windows + miniconda 下实测踩过的坑。面向要安装、部署本 MCP server 的用户与维护者。

## 依赖总览

| 层级 | 依赖 | 版本约束 | 用途 |
|---|---|---|---|
| 解释器 | Python | ≥ 3.10（3.12 实测） | 3.12 系列为主力验证版本 |
| 运行时 | mcp | ≥ 1.0, < 2 | MCP SDK（FastMCP、stdio transport），仅 server 层需要 |
| 运行时 | pyyaml | ≥ 6.0 | 解析 `instruments/profiles/*.yaml` 能力声明 |
| 开发 | pytest | ≥ 8.0（经 `.[dev]` extra 安装） | 单元测试与真机 conformance |

依赖面刻意保持很薄：SCPI 传输层（`transport/scpi_lan.py`）是纯标准库的裸 TCP client。**不走 MCP、直接当 Python 库使用（`open_scope()` + `helpers/`）时只需要 pyyaml，不需要 mcp**——适合自动化脚本、CI 硬件在环测试等非 agent 场景。

## 为什么 mcp 有 <2 上限

mcp 2.0 将 `FastMCP` 更名为 `MCPServer` 并移除了 `mcp.server.fastmcp` 模块，而 `server.py` 的 `from mcp.server.fastmcp import FastMCP` 是 v1 写法——在无上界的 `mcp>=1.0` 声明下，全新安装会解析到 2.x 并在启动时即失败（2026-09 实测 2.2.0，导入阶段报 `ModuleNotFoundError`）。故收紧为 `mcp>=1.0,<2`；待代码迁移到 v2 API 后再放开上界。

## 为什么建议专用环境

mcp SDK 的传递依赖不少（anyio、httpx/httpcore、starlette、uvicorn、pydantic、pydantic-settings、python-dotenv、httpx-sse 等），而 MCP server 一旦注册，就是所有引用它的项目共同依赖的"基础设施"。装进日常环境意味着任何一次无关的 `pip install` 或依赖升级都可能让它起不来，且故障表现为宿主侧"MCP 连接失败"，不容易定位到根因。专用 venv / conda env 让升级变成显式动作，出问题可整体回滚。

## 建立步骤

venv 路线：

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"            # 仅部署运行可省去 [dev]
```

conda 路线（Windows + miniconda 实测）：

```bash
conda create -n scope-mcp --override-channels -c conda-forge python=3.12 -y
conda run -n scope-mcp python -m ensurepip --upgrade    # conda-forge 的 python 不带 pip
conda run -n scope-mcp python -m pip install -e "路径/to/oscilloscope-mcp[dev]"
```

装好后，MCP 宿主注册的 `command` 指向该环境 Python 的**绝对路径**（见 README「注册到 MCP 宿主」一节）。

## Windows（miniconda）实测坑

1. 新版 conda 对 Anaconda defaults 源强制要求接受 Terms of Service；不宜由安装脚本替用户接受法律条款，建环境改用 `--override-channels -c conda-forge` 绕开（`.condarc` 已是 conda-forge 优先时无需额外配置）。
2. conda-forge 的 python 包不含 pip，需 `python -m ensurepip --upgrade` 补齐。
3. conda-forge CDN 与 PyPI（files.pythonhosted.org）偶发连接超时，重试即可恢复。
4. GUI 启动的 MCP 宿主与 shell 的 `PATH` 可能不同，注册配置里的 `command` 写绝对路径最稳。

## 版本验证记录

| 组合 | 结果 |
|---|---|
| Python 3.12.14 + mcp 1.30.0 + pyyaml 6.0.3 + pytest 9.1.1 | 单元测试 593 passed / 90 skipped（skip = 需真机的 conformance 用例）；ZDS1104 真机抓屏通过（2026-09-17） |
| Python 3.12.13 + mcp 1.16.0 | 早期环境长期使用，功能正常 |
| mcp 2.2.0 | 导入阶段即失败（`mcp.server.fastmcp` 不存在），已由 `<2` 上界挡在依赖解析之外 |

## 相关文档

- README.zh.md「注册到 MCP 宿主」：连接配置（`SCOPE_MCP_*` 环境变量）写在哪、怎么注册到宿主
- AGENTS.md（仓库内）：本机专用环境的实际路径与命令

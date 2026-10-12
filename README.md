# ESI Highly Cited Paper Monitor

一个本地运行的 ESI 高被引论文监控工具。它可以从登录后的 Clarivate ESI 页面抓取官方 citation threshold（bar），从 Web of Science Researcher Profile 获取论文引用数，并定期判断论文是否达到阈值。

运行结果会同时显示论文题目、publication year、citations、bar、ESI 数据批次和 margin。每次正式检查都会保存历史快照，用于检查论文是否曾经达标。

> [!IMPORTANT]
> 本工具不保存 WoS/Clarivate 密码。登录由你在本地浏览器中完成，会话数据仅保存在 `.wos_session/`，且已被 Git 忽略。

## Quick Start

### 1. 安装

需要 Python 3.10 或更高版本。

```bash
git clone <your-repository-url>
cd <repository-directory>

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Windows PowerShell 激活虚拟环境时使用：

```powershell
.venv\Scripts\Activate.ps1
```

### 2. 填写论文列表

```bash
cp papers.example.csv papers.csv
```

然后编辑 `papers.csv`，每篇论文一行：

```csv
paper_id,title,doi,esi_field,pub_year,manual_citations,target_type,openalex_filter,notes
paper-1,Example Paper Title,10.0000/example.001,Computer Science,2024,,highly_cited,,
```

`papers.csv` 包含私人论文清单，已被 `.gitignore` 忽略，不会被提交。

### 3. 登录 WoS/ESI

```bash
./esi_monitor.py login-wos
```

脚本会打开本地 Chromium。在浏览器中完成 Clarivate/Web of Science 登录，确认页面可正常访问后，回到终端按 Enter。

如果需要直接登录 WoS Publications 页：

```bash
./esi_monitor.py login-wos \
  --login-url https://www.webofscience.com/wos/op/publications/summary
```

### 4. 运行一次官方检查

```bash
./esi_monitor.py once --official --timeout 60
```

`--official` 会自动：

1. 从 Clarivate ESI 抓取最新官方 bar。
2. 从 WoS Researcher Profile Publications 抓取 `citationCount`。
3. 按 DOI，必要时按题名，匹配 `papers.csv` 中的论文。
4. 按 `citations >= bar` 判断是否达标。
5. 更新当前报告和历史快照。

示例输出：

```text
- [below] paper-1 | Example Paper Title | publication year 2024 |
  citations 42 | bar 65 (ESI snapshot 2026-09-23) | margin -23
```

### 5. 每 24 小时监控

```bash
./esi_monitor.py daemon --official --interval-hours 24 --timeout 60
```

守护进程会重复刷新 bar 和 citations。状态从 `below` 变为 `reached` 时，可通过 webhook 发送提醒。

## 查看历史

```bash
./esi_monitor.py history
./esi_monitor.py history --year 2025
./esi_monitor.py history --paper-id paper-1
./esi_monitor.py history --year 2025 --paper-id paper-1
```

`--year` 是监控快照的日历年，不是论文发表年。历史判定仅使用当时实际保存的 citations 和 bar；工具不会用今天的累计引用数倒推过去的状态。

输出文件：

| 路径 | 内容 |
| --- | --- |
| `reports/esi_status.csv` | 最新一次检查 |
| `reports/esi_history.csv` | 所有已保存的检查快照 |
| `.esi_state.json` | 上一次状态，用于识别首次达标 |
| `esi_thresholds.csv` | 最新阈值表 |

这些文件都是本地数据，默认不会上传到 Git。

## `papers.csv` 字段

| 字段 | 说明 |
| --- | --- |
| `paper_id` | 本地唯一 ID，用于历史追踪 |
| `title` | 论文题目 |
| `doi` | DOI；官方模式优先用它匹配 |
| `esi_field` | ESI 学科名，例如 `Computer Science` |
| `pub_year` | 论文发表年份 |
| `manual_citations` | 仅手动模式需要 |
| `target_type` | `highly_cited` 或 `hot_paper` |
| `openalex_filter` | OpenAlex 估算的可选过滤条件 |
| `notes` | 本地备注 |

CSV 必须是文本文件。不要把 Numbers 或 Excel 文件直接改名为 `.csv`；请使用 Numbers 的 **File > Export To > CSV** 或 Excel 的 **Save As CSV UTF-8**。

## 其他数据模式

### OpenAlex 预警估算

```bash
export OPENALEX_API_KEY="your-openalex-api-key"
export OPENALEX_MAILTO="you@example.com"

./esi_monitor.py once \
  --refresh-thresholds \
  --threshold-source openalex-estimate
```

OpenAlex 的 top 1% 阈值不是 Clarivate ESI 官方阈值，只适合做预警。报告会把这类结果标记为 `estimate_reached` 或 `estimate_below`。

### 手动引用数

先在 `papers.csv` 的 `manual_citations` 中填入数值，并从 `esi_thresholds.example.csv` 创建本地阈值表：

```bash
cp esi_thresholds.example.csv esi_thresholds.csv
./esi_monitor.py once --citation-source manual
```

### 从 CSV URL 刷新阈值

```bash
ESI_THRESHOLD_URL="https://example.com/esi-baselines.csv" \
  ./esi_monitor.py refresh-thresholds --threshold-source url
```

Clarivate `ThresholdsAction.action` 是需要登录的网页，不是稳定的 CSV API。官方模式应使用 `--official`，不要把该网页 URL 当作 `--threshold-url`。

## Webhook 通知

通用 webhook：

```bash
ESI_MONITOR_WEBHOOK_URL="https://example.com/webhook" \
  ./esi_monitor.py daemon --official --interval-hours 24
```

飞书机器人：

```bash
ESI_MONITOR_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/..." \
  ./esi_monitor.py daemon --official --interval-hours 24 --webhook-format feishu
```

默认只在论文首次从未达标变为达标时通知。使用 `--notify-all` 可对每次检查到的已达标论文都发送通知。

## 数据与隐私

以下路径已被 `.gitignore` 排除：

- `.wos_session/`：登录会话、cookie、浏览器缓存和调试页面。
- `papers.csv` / `*.numbers`：私人论文清单。
- `esi_thresholds.csv`：当前抓取的阈值数据。
- `reports/` / `.esi_state.json`：引用数、历史和本地状态。
- `.env*`：本地 API key 或 webhook 配置。

上传前可以用以下命令检查 Git 实际会收录的文件：

```bash
git status --short
git add --dry-run .
```

**不要手动强制添加 `.wos_session/`、`papers.csv` 或 `.env`。**

## 常见问题

### `WoS/ESI session appears to be logged out`

重新登录：

```bash
./esi_monitor.py login-wos \
  --login-url https://www.webofscience.com/wos/op/publications/summary
```

### WoS 出现 `verify you are human`

使用上面的 `login-wos` 命令打开可见浏览器，手动完成验证。

### 论文显示 `missing_citations`

检查 DOI 和题名是否正确，以及论文是否已加入当前 WoS Researcher Profile Publications。未出现在 WoS Profile 时，工具会尝试 OpenAlex 兜底，并在 `citation_source` 中明确标记。

### 页面改版导致解析失败

调试 HTML 会写入 `.wos_session/debug/`。该目录可能包含会话信息，请勿上传或公开。

## 说明

- ESI/WoS 页面结构和访问权限可能变化，自动抓取失败时需要重新登录或更新解析逻辑。
- 请遵守 Clarivate/Web of Science 的许可条款和你所在机构的访问规则。
- 本工具用于本地监控和预警；最终 ESI 认定以 Clarivate 产品中的实际数据为准。

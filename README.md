# repair-codex-history

用于保护和恢复 Codex Desktop 本地历史对话，并在 Windows 上安装一次性的 Codex Pre-launch Guard。

v6 的连续性模式只在启动 Codex 前运行一次：它读取当前有效的 `model_provider`，检查本机用户 Thread 的 provider 元数据，必要时在备份后同步 SQLite 索引和 rollout JSONL，然后启动 Codex。它不常驻后台、不安装服务、不监听配置文件，也不要求切换必须经过 CC Switch。

因此，完成一次初始化后，日常操作是：切换 ChatGPT 账号、中转或 Provider，完全退出 Codex，再从 `Codex Continuity` 快捷方式启动。没有需要同步的变更时，Guard 直接快速退出。

> [!IMPORTANT]
> 本项目只处理当前电脑上仍存在的本地 rollout 文件，不能从云端找回已经删除或未同步到本机的对话。它不读取或修改 `auth.json`、登录 Cookie、API Key、OAuth Token、用户消息、模型回复、工具输出或 `encrypted_content`。

## 用户只需发送一句话

在 Codex 中直接发送下面这句话：

```text
安装 repair-codex-history 项目，并且实现不同账号下的历史会话同步。
```

Codex 应自动完成以下工作：

1. 如果 Skill 尚未安装，调用仓库自举脚本从 `feat/session-continuity-v6` 分支安装本项目，并把 `SKILL.md`、`agents/`、`scripts/` 和 `references/` 安装到当前用户的 Codex Skill 目录；脚本只执行只读 `doctor`，不触碰本地历史。
2. 读取当前有效的 `model_provider`，运行只读诊断。
3. 在 Windows 上运行一次 `bootstrap`：创建快照、同步仍存在本机的用户历史、安装 `Codex Continuity` 快捷方式并复核结果。
4. 在 macOS/Linux 上安装 Skill，并运行安全的 `snapshot` + `repair` 流程；这些平台当前没有仓库内置的自动启动快捷方式。
5. 报告 `next_action`、备份目录和后续只需执行的动作。

首次安装时，如果当前 Codex 进程不能热加载新 Skill，先完全退出并重新打开 Codex，再发送同一句话继续；不要把“已安装”误认为“已完成同步”。如果 Codex 已能加载 Skill 但仍在使用数据库或 writer lock，它会返回 `quit_codex_and_retry`，用户只需完全退出 Codex，再重试或在 Windows 上双击新建的 `Codex Continuity` 快捷方式。这个安全边界不能通过删除 lock 文件绕过。

完成初始化后，日常流程只有：切换账号、中转或 Provider，完全退出 Codex，再从 `Codex Continuity` 启动（Windows），或重新打开 Codex 后让已安装的 Skill 执行 `scan/repair`（macOS/Linux）。如果账号切换没有改变 provider，或两个中转都使用同一个 provider 名称，通常会是 no-op，不会改写历史。

## v6 连续性模式

在 Windows 上，从仓库目录运行：

```powershell
python scripts/repair_history.py doctor --json
python scripts/repair_history.py bootstrap --yes --json
```

`bootstrap` 会先做只读诊断，再创建完整快照，按当前 provider 对齐本地用户历史，安装桌面上的 `Codex Continuity` 快捷方式，并重新校验结果。只有全部步骤成功才会返回：

```json
{"bootstrap_complete": true, "next_action": "none"}
```

如果 Codex 正在运行、存在 writer lock、SQLite 完整性检查失败、schema 不受支持或 rollout 文件缺失，bootstrap 会停止修改并返回明确的 `next_action`。先完全退出 Codex，再按结果重试；不要删除 lock 文件。

安装后：

1. 切换账号、中转或 Provider。
2. 完全退出 Codex。
3. 双击桌面的 `Codex Continuity`。

快捷方式会运行 `guard --yes --json`。成功时返回 `guard_complete: true` 和 `next_action: launch`，然后启动 Codex。Guard 默认只同步 provider metadata，不创建 compatibility alias，也不覆盖 CC Switch 的 endpoint、API Key 或 OAuth 配置。

如果 Guard 返回 `quit_codex_and_retry`，说明 Codex 或 writer lock 仍然存在；返回 `inspect_missing_rollouts` 时，本地文件已缺失，工具不会伪造历史；返回 `unsupported_schema` 时，工具不会猜测 SQL。

## 跨 Provider 的边界

Guard 负责本地历史可见性和 provider metadata 一致性。它会在已知 schema 上同步：

```text
threads.model_provider
session_meta.payload.model_provider
```

它不会保证不同 backend 能解密彼此产生的 `encrypted_content`。因此一个 Thread 可能已经重新出现在历史列表中，但当前 backend 仍无法原地 resume。这属于上游 backend 的兼容性边界，不是本地历史损坏。

遇到这种情况，保留原 Thread，生成一个新的连续性 handoff：

```bash
python scripts/repair_history.py handoff --thread THREAD_ID --json
```

输出目录位于 `~/.codex/history-repair-handoffs/<thread-id>/<timestamp>/`，包含 `HANDOFF.md` 和 `metadata.json`。handoff 只提取可读的用户和 assistant 文本及基本会话元数据，不包含工具输出、隐藏 reasoning、`encrypted_content` 或凭据，原 Thread 永远保留。

账号 A → 账号 B 如果 provider 没有变化，Guard 通常是 no-op；它不读取 `auth.json`，也不判断邮箱。新请求使用当前 Codex 登录身份的配额、权限和模型访问。Relay A → Relay B 如果两者都使用同一个 provider 名称，也通常是 no-op；endpoint 或 Key 的管理仍由 Codex、CC Switch 或用户配置负责。

## v5 命令仍然可用

只读扫描：

```bash
python3 scripts/repair_history.py scan --json
```

创建完整快照：

```bash
python3 scripts/repair_history.py snapshot --yes --json
```

修复到当前 provider：

```bash
python3 scripts/repair_history.py repair --yes --json
```

提前迁移到指定 provider：

```bash
python3 scripts/repair_history.py repair --provider TARGET --yes --json
```

撤销一次 repair、guard 或 bootstrap 产生的 operation manifest：

```bash
python3 scripts/repair_history.py undo --backup /path/to/repair-backup --yes --json
```

`snapshot` 是灾备副本，不是 undo manifest。撤销应使用实际 repair 输出的备份目录；脚本会校验当前 hash，避免覆盖修复后新产生的内容。

## 手动恢复流程

已经发生历史消失、旧对话仍请求旧接口等问题时，可以直接运行：

```bash
python3 scripts/repair_history.py scan --json
python3 scripts/repair_history.py repair --yes --json
```

修复前会检查 SQLite 完整性、rollout 是否存在和 writer lock。每次实际 rewrite 前都会创建备份并记录 SHA-256。rollout 只允许修改 `session_meta.payload.model_provider`；SQLite 只修改已验证 schema 中的 `threads.model_provider`。archived 状态默认保留，内部 subagent Thread 默认排除。

常见 `next_action`：

| 值 | 含义 |
| --- | --- |
| `none` | 已完成，无需继续操作 |
| `restart_to_reload` | 重启 Codex 让侧边栏重新加载 |
| `restart_and_rerun` | 重启后再次 scan/repair，补齐之前被锁定的任务 |
| `quit_codex_and_retry` | 完全退出 Codex 后重试 Guard |
| `inspect_missing_rollouts` | 本地 rollout 已缺失，停止修改 |
| `unsupported_schema` | 数据库结构未知，停止修改 |
| `handoff` | 当前 backend 无法原地续接时生成 handoff |

## 安装 Skill

要求 Python 3.10 或更高版本。

如果用户没有通过 Codex 一句话入口安装，也可以手动从仓库安装：

```bash
git clone https://github.com/Swellyhow/repair-codex-history.git
mkdir -p ~/.codex/skills/repair-codex-history
cp repair-codex-history/SKILL.md ~/.codex/skills/repair-codex-history/
cp -R repair-codex-history/agents repair-codex-history/scripts repair-codex-history/references ~/.codex/skills/repair-codex-history/
```

Windows PowerShell：

```powershell
git clone https://github.com/Swellyhow/repair-codex-history.git
New-Item -ItemType Directory -Force "$HOME\.codex\skills\repair-codex-history" | Out-Null
Copy-Item .\repair-codex-history\SKILL.md "$HOME\.codex\skills\repair-codex-history\" -Force
Copy-Item .\repair-codex-history\agents, .\repair-codex-history\scripts, .\repair-codex-history\references "$HOME\.codex\skills\repair-codex-history\" -Recurse -Force
```

也可以从 [`dist/repair-codex-history.zip`](dist/repair-codex-history.zip) 解压到 `~/.codex/skills/`。安装完成后重新打开 Codex，在对话中使用 `$repair-codex-history`。

## Skill 使用方式

一句话入口等价于下面的 Skill 请求；需要查看或重复初始化时可以直接发送：

```text
使用 $repair-codex-history 安装一次性连续性模式，并同步不同账号下仍存在本机的历史会话。先完成 doctor；Windows 执行 bootstrap，macOS/Linux 执行 snapshot 和 repair。不要安装后台监控程序。
```

已经出现异常时，对 Skill 说：

```text
使用 $repair-codex-history 扫描并恢复切换 Provider 后隐藏的本地对话。
```

## 安全边界

- 不读取、解析、备份或修改 `auth.json`。
- 不记录 API Key、OAuth Token、Cookie、邮箱或完整配置。
- 不修改用户消息、assistant 正文、tool output 或 `encrypted_content`。
- 不删除 writer lock，不杀 Codex，不强制覆盖 active rollout。
- 不自动 unarchive，不覆盖当前 Provider 配置，不把旧 endpoint 复制成当前配置。
- 不从云端下载不存在本机的 Thread，也不绕过账号或模型权限。

## 项目结构

```text
repair-codex-history/
├── README.md
├── SKILL.md
├── repair-codex-history-v6-final-design.md
├── agents/
│   └── openai.yaml
├── scripts/
│   ├── install_from_github.ps1
│   ├── install_from_github.sh
│   ├── repair_history.py
│   ├── install_windows.ps1
│   └── launch_codex_with_guard.ps1
├── references/
│   ├── continuity-architecture.md
│   └── troubleshooting.md
├── tests/
│   └── test_repair_history_v6.py
└── dist/
    ├── repair-codex-history.skill
    └── repair-codex-history.zip
```

完整设计记录见 [`repair-codex-history-v6-final-design.md`](repair-codex-history-v6-final-design.md)。

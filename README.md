# repair-codex-history

用于保护和恢复 Codex Desktop 本地历史对话的 Skill。它可以在切换账号、模型服务商或升级 Codex 前创建完整快照，也可以在对话从侧边栏消失、旧对话仍调用旧接口时执行修复。

> [!IMPORTANT]
> 本工具只能处理当前电脑上仍然存在的 Codex 本地对话文件，不能从云端找回已经删除或未同步到本机的对话。它不会读取或修改 `auth.json`、登录 Cookie、API Key、对话消息和工具输出。

## 新手快速恢复

如果你已经切换账号，并且以前的对话不见了：

1. 安装本 Skill，然后完全退出并重新打开 Codex。
2. 新建一个对话，发送：

   ```text
   使用 $repair-codex-history 恢复切换账号后隐藏的所有本地对话
   ```

3. Skill 会先扫描，再自动备份和修复。
4. 根据返回的 `next_action` 操作：

   | `next_action` | 需要执行的操作 |
   | --- | --- |
   | `restart_and_rerun` | 完全退出 Codex，重新打开后再次发送相同的恢复指令 |
   | `restart_to_reload` | 重启 Codex 一次，让侧边栏重新载入，无需再次修复 |
   | `repair` / `repair_again` | 再执行一次修复 |
   | `inspect_missing_rollouts` | 本地会话文件缺失，停止修改并检查备份 |
   | `none` | 已完成，不需要继续操作 |

当结果同时显示以下内容时，修复完成：

```text
repair_complete: true
next_action: none
```

## 安装

### 使用压缩包安装

1. 从 [`dist/`](dist/) 下载 `repair-codex-history.zip`。
2. 将压缩包解压到 Codex Skills 目录。

macOS 或 Linux：

```bash
mkdir -p ~/.codex/skills
unzip ~/Downloads/repair-codex-history.zip -d ~/.codex/skills
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force "$HOME\.codex\skills" | Out-Null
Expand-Archive "$HOME\Downloads\repair-codex-history.zip" "$HOME\.codex\skills" -Force
```

安装完成后应存在：

```text
~/.codex/skills/repair-codex-history/SKILL.md
```

重新打开 Codex，即可通过 `$repair-codex-history` 使用。

### 从仓库安装

也可以克隆仓库并将 Skill 文件放入 Codex Skills 目录：

```bash
git clone https://github.com/Swellyhow/repair-codex-history.git
mkdir -p ~/.codex/skills/repair-codex-history
cp repair-codex-history/SKILL.md ~/.codex/skills/repair-codex-history/
cp -R repair-codex-history/agents repair-codex-history/scripts ~/.codex/skills/repair-codex-history/
```

要求 Python 3.10 或更高版本。

## 切换前保护

如果还没有切换账号、provider 或升级 Codex，先发送：

```text
使用 $repair-codex-history 在切换前保护我的所有本地对话
```

Skill 会创建经过校验的完整快照，包括：

- SQLite 状态数据库的在线备份
- 所有用户对话 JSONL 文件
- 当前 `config.toml`
- 每个文件的 SHA-256 校验信息
- 快照 manifest

只有看到 `snapshot_complete: true` 后再继续切换。快照目录位于：

```text
~/.codex/history-repair-backups/
```

不要将该备份目录上传到公开仓库，其中可能包含本地项目路径和 provider 地址。

### 切换模型服务商

如果准备切换到另一个 provider，应先在 `config.toml` 中配置目标 provider，然后发送：

```text
使用 $repair-codex-history 在切换到 TARGET 前保护并迁移我的本地对话
```

Skill 会提前迁移未锁定对话，并为仍在运行的旧会话保留兼容映射。完成 provider 切换并重启 Codex 后，再运行一次扫描，根据 `next_action` 完成剩余步骤。

## 恢复后使用哪个账号

完成修复并重启 Codex 后，继续旧对话时使用的是：

```text
旧对话的本地上下文 + 当前登录的新账号
```

新请求的配额、计费、模型权限和身份属于当前账号。使用自定义 provider 时，请求使用当前 `config.toml` 中配置的接口和凭据。Skill 不会复制旧账号凭据。

## 命令行使用

通常只需在 Codex 中用自然语言调用 Skill。需要手动排查时，可在仓库目录执行以下命令。

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

撤销一次修复：

```bash
python3 scripts/repair_history.py undo --backup /path/to/repair-backup --yes --json
```

快照 manifest 不能用于 `undo`。撤销必须使用 `operation: repair` 的 manifest，这样才能避免覆盖修复后新产生的对话。

## 工作原理

Codex 会在 SQLite 中保存对话索引，同时在 rollout JSONL 的 `session_meta.payload.model_provider` 中保存 provider 信息。切换账号或 provider 后，这两处信息可能不一致，导致对话被隐藏，或者继续请求时仍调用旧地址。

本 Skill 会：

1. 检查 SQLite 完整性和本地 rollout 文件。
2. 排除内部 subagent 任务。
3. 跳过正在写入的 rollout 文件。
4. 在修改前逐文件备份并记录修改前后 SHA-256。
5. 只修改 `session_meta.payload.model_provider`。
6. 同步 SQLite 对话索引。
7. 必要时为旧 provider 创建指向当前接口的兼容配置。
8. 使用原子替换写入文件，并提供基于 manifest 的安全撤销。

普通对话消息和工具输出保持原始字节不变。

## 常见问题

### 为什么需要运行两次

Codex 正在使用的对话会有 writer lock。Skill 不会修改活动文件，因此第一次修复会跳过它们。重启后锁会释放，再运行一次即可补齐。

### 修复后重启还会丢失吗

当 `repair_complete` 为 `true` 时，SQLite 和 rollout provider 元数据已经一致，Codex 重建索引时不会再因为旧 provider 而隐藏这些对话。

### 会不会修改我的聊天内容

不会。修复只修改 provider 元数据，不修改用户消息、模型回复或工具输出。

### 可以找回另一台电脑或云端删除的对话吗

不可以。只有本机 rollout 文件仍然存在时才能恢复。

### 会读取旧账号密码或 Token 吗

不会。脚本不会读取或修改 `auth.json`、API Key、Cookie 或登录凭据。

## 项目结构

```text
repair-codex-history/
├── README.md
├── SKILL.md
├── agents/
│   └── openai.yaml
├── scripts/
│   └── repair_history.py
└── dist/
    ├── repair-codex-history.skill
    └── repair-codex-history.zip
```

## 发布校验

当前 v5 发布包 SHA-256：

```text
e62d867038b81e35da695d1b2bdf87d7ebe619ff0899cb5fef8168de10df53cb
```

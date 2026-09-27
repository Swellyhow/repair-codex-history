# repair-codex-history v6 最终升级方案
## 目标：账号 / 中转 / Provider 任意切换后，重启 Codex 即可继续使用本机历史任务

> 项目：`Swellyhow/repair-codex-history`  
> 方案状态：Final Design  
> 面向实现者：Codex / 项目维护者  
> 核心原则：**不常驻后台、不监听 CC Switch、不绑定某一种切换工具；只在 Codex 启动前做一次安全同步。**

---

# 1. 项目背景

当前 `repair-codex-history` v5 已经能够处理 Codex Desktop 本地历史在账号切换、模型 Provider 切换之后出现的典型问题，包括：

- 本地历史文件仍然存在，但侧边栏不显示；
- SQLite 中的 `threads.model_provider` 与 rollout JSONL 中的 `session_meta.payload.model_provider` 不一致；
- 旧 Thread 恢复后仍然请求旧 Provider；
- 正在运行的 Thread 存在 writer lock，不能安全修改；
- 修复前创建备份、保存 SHA-256，并通过 manifest 实现安全撤销；
- 不读取或修改 `auth.json`、API Key、Cookie、用户消息及工具输出。

v5 的核心工作模式仍然是：

```text
发生问题
↓
用户运行 Skill
↓
scan
↓
repair
↓
必要时重启后再次 repair
```

v6 的目标不是推翻这些能力，而是在 v5 基础上增加一个新的“连续性层”，把体验变成：

```text
第一次：
安装 Skill
↓
执行一次 bootstrap
↓
安装非后台的 Pre-launch Guard

以后：
任意方式切换账号 / 中转 / Provider
↓
完全退出 Codex
↓
重新启动 Codex
↓
Guard 在 Codex 启动前自动检查和同步
↓
Codex 启动
↓
历史仍然可见
↓
兼容的 Thread 直接继续
```

Skill 本身只需要负责第一次安装和初始化。

---

# 2. 最终目标

## 2.1 必须覆盖的切换场景

v6 必须覆盖：

1. ChatGPT 账号 A → ChatGPT 账号 B；
2. ChatGPT 账号 B → ChatGPT 账号 A；
3. 第三方中转 A → 第三方中转 B；
4. 第三方中转 B → 第三方中转 A；
5. ChatGPT 官方账号 → 第三方中转；
6. 第三方中转 → ChatGPT 官方账号；
7. CC Switch 发起的 Provider 切换；
8. Codex 自己完成的 ChatGPT 退出登录 / 重新登录；
9. 用户手工修改 `config.toml`；
10. 未来其他工具修改 Codex Provider 配置。

**实现不允许依赖“切换动作必须经过 CC Switch”。**

系统只认最终的 Codex 本地有效状态。

---

# 3. 最重要的设计变化

## 3.1 不再做后台监控

不得使用以下方案作为默认架构：

```text
常驻 python.exe
文件系统 watcher
定时轮询 config.toml
后台 Windows Service
后台 Task Scheduler 每 N 秒扫描
```

原因：

- 没必要长期占用后台进程；
- 容易和 CC Switch 写配置发生竞争；
- 容易和 Codex 正在写 SQLite / rollout 时发生竞争；
- 用户已经接受切换后手动完全退出并重新打开 Codex；
- “Codex 尚未启动”的时间窗口是最安全的修复窗口。

---

## 3.2 改成 Pre-launch Guard

新增一个**非后台、一次性执行的启动前 Guard**。

逻辑：

```text
用户启动 Codex
↓
Pre-launch Guard 运行
↓
读取当前有效 Provider
↓
扫描本地历史
↓
若无需同步：立即结束
↓
若需要同步：
    备份
    同步 provider metadata
    校验
↓
启动 Codex
↓
Guard 进程退出
```

因此：

- 没有常驻 Python；
- 没有持续监听；
- 没有轮询；
- 每次只在 Codex 启动前运行一次；
- 没有切换时应当是快速 no-op。

---

# 4. 不应该再把 CC Switch 当作唯一入口

CC Switch 是重要兼容对象，但不是控制平面。

架构必须是：

```text
             切换来源
                 │
 ┌───────────────┼────────────────┐
 │               │                │
CC Switch     Codex 原生登录     手工/其他工具
 │               │                │
 └───────────────┼────────────────┘
                 ↓
        Codex 最终本地配置
                 ↓
          Pre-launch Guard
                 ↓
        repair-codex-history
```

Guard 不需要知道：

- 谁完成了切换；
- 点击了哪个 CC Switch Provider；
- 用户换成了哪个邮箱；
- OAuth token 是什么；
- API Key 是什么。

Guard 只需要知道：

> “Codex 下一次真正启动时，当前有效的 `model_provider` 是谁？”

---

# 5. 为什么账号切换不需要读取 auth.json

ChatGPT 账号 A → B 时，很多情况下：

```text
model_provider = openai
```

并不会变化。

如果历史 Thread 也已经属于：

```text
openai
```

则账号切换本身并不会导致 provider bucket 不一致。

因此 v6 **不要为了判断账号变化去读取 `auth.json`**。

继续保持当前项目的重要安全承诺：

> 不读取、不解析、不备份、不修改 `auth.json`。

这样：

```text
账号 A → 账号 B
provider 未变化
↓
Guard scan
↓
发现所有历史仍属于当前 provider
↓
no-op
↓
直接启动 Codex
```

如果某种切换方式同时导致：

```text
openai → custom
```

则 Guard 会因为 Provider 变化自动处理。

不需要知道账号身份。

---

# 6. Provider continuity 的核心模型

v6 的“可见性优先”模式采用：

> **Codex 启动前，把本机用户 Thread 的 Provider metadata 同步为“当前即将启动的 Provider”。**

例如：

```text
昨天：
current provider = openai

Thread 1 = openai
Thread 2 = openai
Thread 3 = openai
```

今天用户通过 CC Switch 切到：

```text
current provider = custom
```

重新启动 Codex 前：

```text
Guard
↓
发现：
current = custom

Thread 1 = openai
Thread 2 = openai
Thread 3 = openai
↓
建立 repair backup
↓
全部迁移到 custom
↓
启动 Codex
```

以后切回官方：

```text
current = openai
↓
Guard
↓
必要时迁回 openai
↓
启动 Codex
```

这样无论切换动作来自哪里：

```text
历史列表始终尽可能与“当前 Provider”一致
```

---

# 7. 与 CC Switch Unified Codex Session History 的关系

CC Switch v3.16.x 及之后提供 `Unified Codex session history`。

其核心也是统一 `model_provider` bucket。

v6 应将其视为：

> **优化路径，而不是硬依赖。**

### 已开启 Unified History

很多切换后：

```text
current provider = custom
Thread provider = custom
```

Guard 会直接：

```text
no-op
```

### 未开启 Unified History

例如：

```text
官方 = openai
第三方 = custom
```

Guard 会在每次 `openai ↔ custom` 切换后自动迁移 metadata。

因此：

```text
有 CC Switch Unified History
→ 更少改文件

没有 CC Switch Unified History
→ v6 仍然能工作
```

---

# 8. v6 不应自动修改 CC Switch Provider 配置

CC Switch 管理：

```text
config.toml
Provider endpoint
API Key 配置
OAuth / 官方认证模式
模型列表
```

v6 不应该和 CC Switch 抢配置控制权。

Pre-launch Guard 默认：

```text
只读取 config.toml
↓
解析当前有效 model_provider
↓
同步历史
```

**不得默认覆盖：**

- `base_url`
- API Key
- OAuth 配置
- `requires_openai_auth`
- CC Switch 当前 Provider
- 当前模型列表

现有 v5 的 compatibility alias 能力必须保留给手动 repair 模式，但：

> `guard` 默认不创建 Provider alias。

---

# 9. 新增命令设计

保留现有：

```bash
scan
snapshot
repair
undo
```

新增：

```bash
doctor
bootstrap
guard
handoff
```

---

# 10. doctor

命令：

```bash
python scripts/repair_history.py doctor --json
```

必须是纯只读。

输出至少包括：

```text
codex_home
database_path
database_schema_supported
sqlite_integrity
current_provider
session_count
user_thread_count
archived_thread_count
internal_thread_count
missing_rollout_count
writer_lock_count
provider_mismatch_count
cc_switch_detected
cc_switch_unified_history_detected
guard_installed
guard_version
safe_to_bootstrap
recommended_action
```

不得读取 `auth.json`。

---

# 11. bootstrap

第一次安装 v6 时运行：

```bash
python scripts/repair_history.py bootstrap --yes --json
```

流程：

```text
doctor
↓
完整 snapshot
↓
确定当前 provider
↓
扫描所有本机用户 Thread
↓
安全迁移到当前 provider
↓
验证 SQLite + rollout 一致性
↓
安装 Pre-launch Guard
↓
输出 bootstrap_complete
```

只有全部成功才能：

```json
{
  "bootstrap_complete": true
}
```

如果发现 SQLite integrity error、未知 schema、rollout 缺失、无法确定 Provider、Codex 仍在运行或 writer lock 未释放，必须停止修改并返回明确 `next_action`。

---

# 12. guard

Guard 是 v6 的核心。

命令：

```bash
python scripts/repair_history.py guard --yes --json
```

启动器每次启动 Codex 前调用。

## 12.1 Fast path

如果：

```text
hidden/mismatched user threads = 0
```

直接：

```json
{
  "changed": false,
  "guard_complete": true,
  "next_action": "launch"
}
```

不得建 backup、rewrite JSONL、rewrite SQLite、修改 config 或扫描所有消息正文。

目标：

> 没有切换时 Guard 几乎无感。

## 12.2 Repair path

发现：

```text
thread.model_provider != current_provider
```

时：

```text
重新检查 Codex 是否完全退出
↓
重新检查 writer lock
↓
SQLite integrity_check
↓
建立 differential repair backup
↓
修改 rollout：
session_meta.payload.model_provider
↓
修改 SQLite：
threads.model_provider
↓
校验
↓
写 manifest
↓
返回 launch
```

Guard 与现有 repair 必须共享底层事务实现，不能复制一套不同的修复逻辑。

---

# 13. Guard 绝不能修改的内容

无论任何模式：

```text
用户消息
模型回复
tool call 内容
tool output
encrypted_content
auth.json
API Key
OAuth Token
Cookie
项目源码
```

全部不得修改。

---

# 14. writer lock 策略

Guard 应：

1. 检查 Codex 是否正在运行；
2. 检查 writer locks；
3. 如果存在，等待有限时间；
4. 再检查；
5. 仍存在则停止修改。

建议默认最多等待 5 秒。

不能删除 lock、kill Codex 或强制覆盖 active rollout。

返回：

```json
{
  "guard_complete": false,
  "next_action": "quit_codex_and_retry"
}
```

---

# 15. Pre-launch Launcher

Windows 为第一优先级。

新增：

```text
scripts/install_windows.ps1
scripts/launch_codex_with_guard.ps1
```

推荐流程：

```text
Windows Shortcut
↓
launch_codex_with_guard.ps1
↓
python repair_history.py guard --yes --json
↓
如果安全：
启动 Codex
↓
PowerShell / Python 进程退出
```

---

# 16. Codex 启动方式不能硬编码

实现者必须自动发现 Codex Desktop。

优先考虑：

- Windows Start Apps / AppUserModelID；
- 已安装 executable；
- 已存在用户 Codex shortcut；
- 用户明确提供的启动命令。

不得硬编码用户名路径。

如果无法可靠找到 Codex，允许要求用户选择/确认一次 Codex 启动目标。

保存的是启动目标，不是认证数据。

---

# 17. Skill 只执行一次

用户理想流程：

```text
第一次：

安装 $repair-codex-history
↓
“安装连续性模式”
↓
bootstrap
↓
安装 Guard shortcut
↓
结束
```

以后不需要再调用 Skill。

日常：

```text
切换
↓
完全退出 Codex
↓
从 Codex Continuity shortcut 打开
↓
自动 Guard
↓
Codex 打开
```

---

# 18. 不默认替换原 Codex Shortcut

默认创建：

```text
Codex Continuity
```

不要默认删除用户原来的快捷方式。

可提供：

```text
--replace-shortcut
```

只有用户明确选择后才替换。

---

# 19. account → account 场景

## ChatGPT A → ChatGPT B

如果 Provider 不变：

```text
openai → openai
```

或：

```text
custom → custom
```

则：

```text
Guard = no-op
```

本地历史可见性不依赖邮箱。

新请求使用当前登录账号的配额、权限、模型访问和计费身份。

repair-codex-history 不复制旧账号认证。

---

# 20. relay → relay 场景

例如：

```text
中转 A
model_provider = custom

↓

中转 B
model_provider = custom
```

虽然 `base_url`、API Key、model 可能变化，但历史 bucket 未变化。

因此 Guard 通常：

```text
no-op
```

不能简单把：

```text
config.toml hash changed
```

理解为：

```text
必须迁移历史
```

历史可见性的核心判断是 `current model_provider`。

---

# 21. official ↔ relay 场景

最典型需要 Guard 的情况：

```text
openai ↔ custom
```

Guard 自动同步 metadata。

用户不需要再次运行 Skill、再次输入 repair 指令、手工 SQLite 或手工改 JSONL。

---

# 22. encrypted_content：必须明确承认的上游限制

历史可见：

```text
≠
```

跨 backend 一定可以原地续聊。

Codex session 中可能存在：

```text
encrypted_content
```

某些 reasoning ciphertext 只能由生成它的 backend 解密。

因此：

```text
Provider A 创建 Thread
↓
迁移 metadata 到 Provider B
↓
Thread 出现在列表里
↓
Resume 时 Provider B 无法解密旧 encrypted_content
↓
继续失败
```

这不是 history corruption。

不得：

- 声称 100% 跨 Provider 无损续聊；
- 自动删除 encrypted_content；
- 自动篡改 reasoning；
- 为了“能继续”而破坏原 session。

---

# 23. handoff fallback

新增：

```bash
python scripts/repair_history.py handoff --thread THREAD_ID --json
```

只在原地 resume 失败时使用。

目标：

```text
保留原 Thread
↓
从 rollout 中提取可读上下文
↓
生成 continuity handoff
↓
供当前 Provider 建立新 Thread
```

输出：

```text
~/.codex/history-repair-handoffs/<thread-id>/<timestamp>/
├── HANDOFF.md
├── metadata.json
└── source-manifest.json
```

---

# 24. HANDOFF.md 内容

尽可能包括：

```text
原 Thread ID
原 cwd
原模型信息（如可用）
任务目标
用户最近的重要要求
已经完成的工作
当前项目状态
关键文件路径
关键工具结果
尚未完成事项
最近若干轮可读对话
```

不得包含：

```text
encrypted_content
OAuth Token
API Key
auth.json 内容
隐藏 reasoning
```

---

# 25. handoff 不得删除原 Thread

原始 session 永远保留。

handoff 是：

```text
新建连续性入口
```

不是覆盖、清洗或替换旧历史。

v6 第一阶段建议只生成 `HANDOFF.md`，不要依赖不稳定的 Desktop 内部 Thread 创建接口。

---

# 26. 保留 v5 所有能力

以下接口不得删除：

```bash
scan
snapshot
repair
undo
```

包括：

```bash
python scripts/repair_history.py repair --provider TARGET --yes --json
```

必须继续兼容。

---

# 27. 推荐项目结构

```text
repair-codex-history/
├── README.md
├── SKILL.md
├── agents/
│   └── openai.yaml
├── scripts/
│   ├── repair_history.py
│   ├── install_windows.ps1
│   └── launch_codex_with_guard.ps1
├── references/
│   ├── continuity-architecture.md
│   └── troubleshooting.md
├── tests/
│   ├── test_scan.py
│   ├── test_repair.py
│   ├── test_guard.py
│   ├── test_bootstrap.py
│   ├── test_handoff.py
│   └── fixtures/
└── dist/
    ├── repair-codex-history.skill
    └── repair-codex-history.zip
```

---

# 28. repair_history.py 内部重构要求

不要把 scan、repair、guard、bootstrap 写成四套重复逻辑。

拆成复用层：

```text
discover_codex_home()
discover_state_db()
inspect_schema()
determine_current_provider()
enumerate_user_threads()
enumerate_internal_threads()
inspect_rollout()
inspect_writer_lock()

create_snapshot()
create_repair_backup()

build_repair_plan()
apply_repair_plan()
verify_repair()

create_manifest()
undo_manifest()

create_handoff()
```

然后：

```text
repair = build + apply
guard = fast scan + build + apply
bootstrap = doctor + snapshot + guard + install
```

---

# 29. 修复事务要求

一次 Guard repair 必须遵守：

```text
PLAN
↓
RECHECK LOCKS
↓
BACKUP
↓
WRITE JSONL
↓
UPDATE SQLITE TRANSACTION
↓
VERIFY
↓
WRITE MANIFEST
```

如果任何一步失败，不得伪装为成功。

如果已经部分写入，必须能根据 operation manifest 回滚。

---

# 30. JSONL 修改范围

依旧只允许修改：

```text
session_meta.payload.model_provider
```

普通事件保持不变。

除目标 metadata 字段外，不得发生其他变化。

---

# 31. SQLite 修改范围

目标：

```text
threads.model_provider
```

如果未来 schema 改变：

```text
doctor
↓
unsupported_schema
↓
停止
```

不得在未知 schema 上执行猜测式 SQL。

---

# 32. archived / internal threads

默认保留 archived 状态。

Guard 只解决 provider visibility，不得自动 unarchive。

内部 subagent thread 默认排除，不作为普通用户历史迁移。

---

# 33. SQLite 路径兼容

不得只假设：

```text
~/.codex/state_5.sqlite
```

要兼容：

```text
sqlite_home
自定义 CODEX_HOME
未来目录布局
```

优先复用现有 v5 discovery 逻辑。

---

# 34. Fast Guard 性能目标

没有切换时不得遍历每个 JSONL 的完整内容。

优先：

```text
SQLite query
↓
mismatch = 0
↓
立即退出
```

建议：

```text
普通机器 < 1 秒
大型历史库尽量 < 2 秒
```

只有发现 mismatch 时才做 rollout 深度验证。

---

# 35. 安全状态文件

可以保存：

```text
~/.codex/history-repair-state/guard-state.json
```

只允许包含：

```text
last_provider
last_guard_time
last_guard_version
last_manifest
last_result
```

不要保存账号邮箱、API Key、Token、auth.json hash 或完整 config.toml。

Guard 是否运行的判断不能完全依赖 state file；SQLite / rollout 才是事实来源。

---

# 36. CC Switch 共存原则

检测到 CC Switch 时：

```text
只报告
不接管
```

例如 doctor：

```json
{
  "cc_switch_detected": true,
  "unified_history_detected": true
}
```

如果 Unified History 已正常工作：

```text
Guard no-op
```

不要二次重写。

---

# 37. bootstrap 与 CC Switch Unified History

如果检测到 Unified History 已启用，并且：

```text
current provider = custom
```

bootstrap 应：

```text
snapshot
↓
扫描
↓
只修复残留 openai mismatch
↓
安装 Guard
```

不要重复实现 CC Switch 已完成的配置注入。

---

# 38. 统一 next_action

```text
none
launch
quit_codex_and_retry
repair
repair_again
restart_to_reload
inspect_missing_rollouts
unsupported_schema
restore
handoff
```

Guard 专用：

```text
launch
quit_codex_and_retry
inspect_missing_rollouts
unsupported_schema
```

---

# 39. Guard 失败时的行为

### writer lock

提示：

> Codex 可能仍在运行，请完全退出后重新打开。

不得强行启动第二个实例。

### SQLite integrity failure / unknown schema

Guard 必须停止修复。

可以提供“直接启动 Codex（不修复）”，但必须明确显示 continuity guard 未完成。

---

# 40. backup 策略

bootstrap：

```text
Full Snapshot
```

日常 Guard：

```text
Differential Repair Backup
```

无变化：

```text
0 backup
```

有变化：

```text
只备份将修改的 rollout + 必要 DB 状态
```

---

# 41. manifest

每次实际 Guard repair 都生成 manifest，至少包含：

```text
operation
timestamp
version
target_provider
sqlite_path
sqlite_before_hash
sqlite_after_hash
changed_thread_ids
changed_rollout_files
rollout_before_hash
rollout_after_hash
skipped_locked_threads
verification_result
```

不得记录 secret。

---

# 42. undo

现有 `undo --backup ...` 必须能够撤销：

```text
manual repair
guard repair
bootstrap migration
```

但 Full Snapshot 仍然不直接等价于 operation undo。

继续遵守：

> Snapshot 是灾备副本；Undo 必须使用 operation manifest。

---

# 43. 日志

建议：

```text
~/.codex/history-repair-logs/
```

只记录：

```text
guard timestamp
current provider
mismatch count
changed count
duration
result
manifest path
```

不得记录 prompt、对话正文、Token、API Key 或 auth.json。

---

# 44. 用户最终使用方式

## 第一次

用户对 Skill 说：

```text
使用 $repair-codex-history 安装一次性连续性模式，
以后无论我切换 ChatGPT 账号、中转还是 Provider，
都在重新启动 Codex 前自动同步本地历史。
不要安装后台监控程序。
```

Skill：

```text
doctor
↓
bootstrap
↓
安装 Guard shortcut
↓
验证
```

## 以后

用户只需要：

```text
1. 切账号 / 中转 / Provider
2. 完全退出 Codex
3. 打开 Codex Continuity
```

不需要再运行 Skill。

---

# 45. 不经过 CC Switch 的 ChatGPT 账号切换

必须作为正式测试场景，不是 edge case。

测试：

```text
ChatGPT Account A
↓
创建 Thread A
↓
退出 Codex
↓
在 Codex 内退出 A
↓
登录 Account B
↓
退出 Codex
↓
使用 Guard 启动
```

验收：

```text
Thread A 仍然存在于本地历史
```

若 Provider bucket 没变化，Guard 不应修改 Thread。

若 Provider bucket 因登录流程发生变化，Guard 自动同步。

---

# 46. account-to-account encrypted_content

不要预设“换账号一定失败”，也不要承诺“换账号一定成功”。

正确边界：

```text
历史可见性
→ Guard 负责

运行时 encrypted_content 是否被当前 backend 接受
→ 由 Codex / backend 决定
```

如果出现已知 resume 错误，建议 handoff。

---

# 47. 完整测试矩阵

| 场景 | 历史可见 | Guard 是否改 metadata | Resume 预期 |
|---|---|---:|---|
| Official A → Official B，provider 不变 | 必须 | 否 | 尽量原地继续 |
| Official A → Official B，provider 改变 | 必须 | 是 | 尽量原地继续 |
| Relay A → Relay B，均 custom | 必须 | 否 | 取决于 backend |
| OpenAI → custom | 必须 | 是 | 可能 encrypted_content 失败 |
| custom → OpenAI | 必须 | 是 | 可能 encrypted_content 失败 |
| CC Switch Unified History custom → custom | 必须 | 否 | 取决于 backend |
| 无任何切换 | 必须 | 否 | 原样 |
| writer lock 存在 | 不损坏 | 否 | 要求退出 |
| rollout 缺失 | 不伪造 | 否 | inspect |
| SQLite 损坏 | 不修改 | 否 | stop |
| unknown schema | 不修改 | 否 | stop |
| archived Thread | 状态保持 | 仅 provider 必要修改 | 原样 |
| internal subagent | 不作为普通历史迁移 | 否 | N/A |

---

# 48. 回归测试：v5 不能被破坏

现有以下功能全部测试：

```text
scan
snapshot
repair
repair --provider
repair --unarchive
repair --index-only
undo
--latest
JSON output
writer-lock handling
compatibility aliases
```

确保 v6 不破坏旧工作流。

---

# 49. 单元测试重点

`test_guard.py` 至少覆盖：

1. same provider → no-op；
2. SQLite mismatch → plan；
3. JSONL mismatch → plan；
4. SQLite + JSONL mismatch → repair；
5. writer lock → refuse；
6. current provider unknown → refuse；
7. missing rollout → refuse；
8. internal thread → skip；
9. archived thread → preserve；
10. second guard → idempotent no-op；
11. undo guard repair → original provider restored；
12. CC Switch unified history → no-op；
13. auth.json 不被打开；
14. config.toml 不被修改；
15. encrypted_content 字节保持不变。

---

# 50. 端到端 Windows 测试

必须在 Windows Codex Desktop 环境测试：

```text
安装
↓
bootstrap
↓
创建 Continuity shortcut
↓
关闭 Codex
↓
切 Provider
↓
用 shortcut 启动
↓
确认历史
↓
继续 Thread
```

至少测试：

```text
OpenAI → custom
custom → OpenAI
custom Relay A → custom Relay B
Official Account A → Official Account B
```

---

# 51. README 更新

README 第一屏应直接解释：

> repair-codex-history v6 可以安装一个非后台的 Codex Pre-launch Guard。  
> Guard 只在启动 Codex 前运行，自动把仍存在本机的历史 Thread 与当前 Provider 对齐。  
> 因此用户可以在 ChatGPT 账号、CC Switch 中转或其他 Provider 之间切换后，完全退出并重新打开 Codex，历史无需每次手工 repair。

随后明确：

> 它保证的是本地历史可见性和 metadata 一致性，不保证不同 backend 能解密彼此产生的 encrypted_content。

---

# 52. SKILL.md 更新

保持：

```yaml
name: repair-codex-history
```

不要更名成 `codex-history-manager`。

description 增加：

```text
installing one-time continuity/pre-launch mode
```

Skill 工作流增加：

```text
用户要求长期无感切换
→ doctor
→ bootstrap
→ install Guard
```

---

# 53. dist 发布

更新：

```text
dist/repair-codex-history.skill
dist/repair-codex-history.zip
```

发布前：

```text
运行完整测试
验证 Skill
重新打包
计算 SHA-256
更新 README
```

---

# 54. GitHub 开发流程

不要直接在 `main` 修改。

创建：

```bash
git checkout -b feat/session-continuity-v6
```

建议分阶段提交：

```text
refactor: extract shared repair planning
feat: add doctor and guard commands
feat: add bootstrap continuity workflow
feat: add Windows pre-launch guard installer
feat: add safe thread handoff
test: add continuity switching matrix
docs: document v6 continuity architecture
```

全部测试通过后再 Pull Request → Review → Merge main。

Codex 不得在没有用户明确许可时 force push 或覆盖 main。

---

# 55. 实施顺序

## Phase 1 — Preserve v5

先 clone、运行测试、阅读 `repair_history.py`、阅读 `SKILL.md`，确认当前行为并建立 baseline。

## Phase 2 — Internal refactor

先抽共享逻辑，不新增外部行为。旧测试必须继续通过。

## Phase 3 — doctor + guard

实现只读 doctor，再实现 guard。先通过模拟文件系统测试，不做 launcher。

## Phase 4 — bootstrap

加入 snapshot、guard、verification、install state。

## Phase 5 — Windows launcher

实现 `install_windows.ps1` 与 `launch_codex_with_guard.ps1`，确保不是后台任务。

## Phase 6 — handoff

最后处理 encrypted_content fallback，不允许通过删除加密内容来“修复”。

## Phase 7 — Docs / Skill / Dist

更新 README、SKILL.md、references、dist、SHA-256。

---

# 56. v6 验收标准

## 数据安全

- [ ] 不读取 auth.json；
- [ ] 不修改 auth.json；
- [ ] 不记录 API Key；
- [ ] 不修改用户消息；
- [ ] 不修改 assistant 正文；
- [ ] 不修改 tool output；
- [ ] 不修改 encrypted_content；
- [ ] 每次实际 rewrite 前有 backup；
- [ ] rewrite 可 undo。

## 连续性

- [ ] OpenAI ↔ custom 历史保持可见；
- [ ] custom A ↔ custom B 历史保持可见；
- [ ] ChatGPT Account A ↔ B 历史保持可见；
- [ ] 切换不要求必须经过 CC Switch；
- [ ] 无切换时 Guard 是 no-op；
- [ ] Skill 安装后日常无需再次调用。

## 稳定性

- [ ] writer lock 不强改；
- [ ] unknown schema 不强改；
- [ ] SQLite integrity failure 不强改；
- [ ] internal subagent 不误迁移；
- [ ] archived 状态不改变；
- [ ] 第二次 Guard 幂等。

## 体验

- [ ] 没有后台 Python；
- [ ] 没有 Service；
- [ ] 没有持续 watcher；
- [ ] 用户只需切换 → 退出 → 重新打开；
- [ ] 快速路径通常 < 1 秒；
- [ ] 原 v5 命令全部兼容。

---

# 57. 明确的非目标

v6 不负责：

1. 将 ChatGPT A 的云端聊天同步到 ChatGPT B；
2. 从 OpenAI 云端下载不存在本机的 Thread；
3. 恢复已经从本机删除的 rollout；
4. 绕过账号权限；
5. 绕过模型权限；
6. 复制 ChatGPT OAuth 凭据；
7. 转移额度；
8. 破解 encrypted_content；
9. 保证任何第三方中转完整兼容 Codex；
10. 保证所有跨 backend Thread 都能原地 resume。

---

# 58. 最终产品定义

最终的 `repair-codex-history` 不再只是：

> “对话消失以后修一下。”

而是：

> **Codex 本地 Session Continuity Layer**

它承担：

```text
保护
扫描
修复
迁移
启动前同步
撤销
handoff
```

同时坚持：

```text
认证完全交给 Codex / CC Switch
```

---

# 59. 给 Codex 的最终执行指令

```text
你现在负责升级 GitHub 项目：

https://github.com/Swellyhow/repair-codex-history

请严格按照本方案实施 repair-codex-history v6。

核心目标：

1. 保留现有 v5 的 scan / snapshot / repair / undo 全部功能和兼容性。
2. 新增 doctor / bootstrap / guard / handoff。
3. 不安装任何常驻后台 Python、Service 或文件监听器。
4. 新增 Windows Pre-launch Guard：
   用户切换 ChatGPT 账号、中转或 Provider 后，只需要完全退出并重新打开 Codex。
   Guard 在 Codex 启动前自动检查当前有效 model_provider，并把本机用户 Thread 的 provider metadata 安全同步到当前 Provider。
5. 不能要求切换必须经过 CC Switch。
6. CC Switch Unified Codex Session History 只作为兼容/优化路径，不作为唯一依赖。
7. 永远不要读取、解析、备份或修改 auth.json。
8. 永远不要修改 API Key、OAuth Token、Cookie。
9. JSONL 仅允许修改 session_meta.payload.model_provider。
10. SQLite 仅在已知并验证过的 schema 上修改 threads.model_provider。
11. 不修改 conversation body、tool output、encrypted_content。
12. 所有实际 rewrite 之前必须备份，并生成可 undo manifest。
13. writer lock、SQLite integrity failure、unknown schema、missing rollout 必须 fail-safe。
14. encrypted_content 跨 backend 不兼容时不得自动删除；提供 handoff fallback。
15. account-to-account 切换必须作为正式测试场景，即使切换动作完全不经过 CC Switch。
16. 新增完整单元测试和 Windows E2E 测试说明。
17. 更新 README、SKILL.md、references、dist 和 SHA-256。
18. 保持 Skill 名称 repair-codex-history，不允许更名。
19. 不要直接修改或 force-push main。创建 feat/session-continuity-v6 分支。
20. 修改前先运行现有项目测试并建立 baseline；每个阶段完成后运行回归测试。

实现过程中不得用“删掉 encrypted_content”作为默认修复方法。
不得为了演示成功而降低安全检查。
如果当前 Codex schema、CC Switch 行为或仓库代码与本文档假设不一致，以实际源码为准，先报告差异，再做最小兼容调整。
```

---

# 60. 实现时必须参考的上游资料

## repair-codex-history

https://github.com/Swellyhow/repair-codex-history

重点：

- 当前 README；
- 当前 SKILL.md；
- `scripts/repair_history.py`；
- v5 backup / manifest / lock / alias 逻辑。

## CC Switch Unified Codex Session History

https://github.com/farion1231/cc-switch/blob/main/docs/guides/codex-unified-session-history-guide-en.md

重点：

- `openai` / `custom` history bucket；
- Unified History；
- migration / backup；
- `encrypted_content` 跨 backend 限制；
- 只改 `model_provider` metadata 的设计原则。

---

# 61. 最后一条原则

优先级始终是：

```text
数据不损坏
>
历史可见
>
可以原地 Resume
>
使用体验自动化
```

如果为了“看起来无缝”必须破坏原始 Session，则：

```text
宁可 handoff
也不要破坏历史。
```

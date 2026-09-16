# kixparadigm → Devin 移植包

把 [kixparadigm](https://github.com/olicesx/kixparadigm) 的范式搬进 Devin CLI/Desktop：认知层规则、机械门禁、多智能体编排 skill、方法论 skills、subagent 角色档。

## 安装

**方式 A — plugin（推荐，云同步全设备）**：

```bash
devin plugins install <本 repo 的 git URL>   # 不加 --local → 记入云端 manifest，所有登录同账号的设备自动加载
```

注意：plugin 方式下 skill 带命名空间前缀（`/kixparadigm:kixpower-new`），且插件 hooks 是 best-effort/fail-open——硬门禁要可靠还需在每台机器跑一次 install.sh。

**方式 B — 本机直装（硬门禁可靠）**：

```bash
./install.sh            # 安装到 ~/.config/devin/，hooks 注册进 config.json
```

目标机（如 `8.209.206.41`）上：`git clone <repo>` 后跑 `./install.sh`。依赖只有 `python3` 和 `bash`（`rsync` 可选，缺失时自动退化为 cp）。

## 内容物

| 组件 | 位置 | 说明 |
|---|---|---|
| 认知层 | `~/.config/devin/AGENTS.md` | kixparadigm 常驻规则：三通道验证/需求三检/闸门证词机制/规则是负债/生成阶段内部充分推理/交付边界 |
| 守卫 hook | `~/.config/devin/hooks/kix_guards.py` | force push、main 直写 commit/push、破坏性 SQL、`gh` 远端删除 → **硬拦**（exit 2）；无进度文档 / commit 频率 → 软提醒（PostToolUse）；Stop 时要求验证完整性 |
| 编排 skills | `skills/kixpower-*` | `/kixpower-new` `/kixpower-import` `/kixpower-continue` `/kixpower-review` |
| 方法论 skills | `skills/` | kixparadigm（认知技能本体）、kixpower（编排方法论）、tdd/diagnose/handoff/triage/teach/caveman/zoom-out/grill-me/prototype/to-prd/to-issues/write-a-skill/improve-codebase-architecture/pwsh-reliable |
| subagent 档 | `agents/` | kixpower-dev / kixpower-qa / kixpower-reviewer / kixpower-reviewer-cross / kixpower-producer |
| 脚本 | `skills/kixpower/scripts/` | `new-sprint.sh`（Sprint 骨架）、`validate-memory-backlog.sh`（L4 backlog 校验） |

## 与原版（DSH/Copilot）的差异

| 原版机制 | Devin 移植状态 |
|---|---|
| cordis JS 插件 | ❌ 不存在；由 `hooks/` Python 脚本 + `config.json` 注册接管 |
| kix-route 运行时厂商路由 | ⚠️ 静态化：subagent profile 的 `model:` 字段钉死；跨厂商观察用 `kixpower-reviewer-cross`（改其 `model:` 字段切厂商） |
| DSH `workflow`/`goal` 工具 | ⚠️ 由主线程 `run_subagent` + `todo_write` + sprint 文档承担 |
| kix-focus 工具面裁剪 | ❌ 不需要（Devin 主线程工具面本来就小；角色边界靠 profile `allowed-tools`） |
| orchestrator agent | ⚠️ 不建行：orchestrator 编排规则在 kixpower skill 内，作用于主线程 |
| 核心 agent 提示词注入 | ⚠️ 去重：各 profile 只保留操作层增量；持久原则以全局 `AGENTS.md` 为唯一事实源 |

## 验证

```bash
# 1) hook 能拦 force push
echo '{"tool_name":"exec","tool_input":{"command":"git push --force origin main"}}' \
  | python3 ~/.config/devin/hooks/kix_guards.py; echo "exit=$?"   # 应 exit=2

# 2) 开 devin 会话输入 /kixpower-new 应被识别为 skill
```

## 卸载

```bash
rm ~/.config/devin/AGENTS.md
rm -rf ~/.config/devin/skills ~/.config/devin/agents/*.md
rm ~/.config/devin/hooks/kix_guards.py
# 再手动从 ~/.config/devin/config.json 的 hooks 段删掉指向 kix_guards.py 的三条
```

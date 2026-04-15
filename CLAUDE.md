# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Decompiled TypeScript source of **Claude Code v2.1.88**, extracted from the npm package `@anthropic-ai/claude-code`. This is a **read-mostly research repository** — not a development repo with CI/tests. The source is incomplete: 108 feature-gated internal modules were dead-code-eliminated at compile time and cannot be recovered.

## Build Commands

```bash
npm run build          # prepare-src + esbuild bundle → dist/cli.js
npm run check          # TypeScript type check (no emit)
npm run start          # Run dist/cli.js
npm run prepare-src    # Transform source for Node.js compatibility (feature() → false, MACRO → literals)
```

Full rebuild requires **Bun** + Anthropic's internal build config. The esbuild-based build is best-effort (~95% coverage). See `QUICKSTART.md` for details on build limitations and missing module stubs.

## Architecture

### Entry Flow

`src/entrypoints/cli.tsx` → `src/main.tsx` (bootstrap, config, auth, React/Ink REPL) → **query loop** in `src/query.ts`

### Query Loop (core agent loop — `src/query.ts`, 70KB)

User input → Claude API call → detect `tool_use` → execute tools (streaming, parallel) → append results → loop until `stop_reason != "tool_use"` → return text to user.

Orchestrated by `src/QueryEngine.ts` (SDK/headless entry point for the same loop).

### Key Modules

| Module | Purpose |
|--------|---------|
| `src/query.ts` | Main agent loop (largest file, ~70KB) |
| `src/main.tsx` | CLI bootstrap + initialization (~808KB) |
| `src/QueryEngine.ts` | Headless query lifecycle engine |
| `src/Tool.ts` | Tool interface + `buildTool` factory |
| `src/commands.ts` | ~80 slash command registrations |
| `src/state/AppState.tsx` | Central React Context state |

### Directory Layout

| Directory | What's Inside |
|-----------|---------------|
| `src/tools/` | 40+ built-in tools (Bash, FileEdit, FileRead, Grep, Agent, MCP, etc.) |
| `src/commands/` | ~80 slash commands (`/config`, `/plan`, `/mcp`, `/memory-save`, etc.) |
| `src/components/` | React/Ink terminal UI components |
| `src/services/` | Core services: API client, MCP, tools executor, analytics, plugins, OAuth, LSP |
| `src/utils/` | 100+ utility modules (auth, config, permissions, tokens, telemetry, etc.) |
| `src/hooks/` | React hooks for tool permissions, notifications |
| `src/constants/` | System prompts, XML tags, config constants |
| `src/skills/` | Bundled skill definitions |
| `src/plugins/` | Plugin system + bundled plugins |
| `src/tasks/` | Background task system (local/remote agents) |
| `stubs/` | Build stubs replacing Bun intrinsics (`feature()`, `MACRO`) |
| `scripts/` | Build scripts (esbuild-based) |
| `docs/` | Deep analysis reports in 4 languages (EN/JA/KO/ZH) |

### Key Architectural Patterns

- **Feature gates**: `feature('FLAG')` from `bun:bundle` — compile-time branching for internal features (DAEMON, KAIROS, BRIDGE_MODE, COORDINATOR_MODE, etc.). Stubbed to `false` in this build.
- **Tool system**: Each tool in `src/tools/` follows the `Tool` interface from `src/Tool.ts` (name, description, inputSchema, execute). Two-layer permission checks (policy + user consent) via `src/hooks/toolPermission/`.
- **Multi-agent**: `AgentTool` spawns sub-agents, `TeamCreateTool`/`SendMessageTool` coordinate them, shared task tracking.
- **State**: React Context in `src/state/AppState.tsx` — model, cwd, permissions, session metadata, plugin/skill config.
- **UI**: React/Ink for terminal rendering (`src/components/`).
- **Context compaction**: Automatic token limit enforcement with message summarization when conversation grows too long.

## Codebase Quirks

- No test files exist — testing lives in Anthropic's internal monorepo
- `src/main.tsx` (808KB) and `src/query.ts` (70KB) are unusually large monolithic files
- Many imports reference 108 missing modules that are feature-gated and eliminated at compile time — expect unresolvable references
- TypeScript config uses `"strict": false` and `"moduleResolution": "bundler"`
- Runtime dependencies (react, ink, chalk, @anthropic-ai/sdk, zod, etc.) are not listed in package.json — they exist in `node_modules/` from the original npm package

---

## 学习计划：从 Claude Code 源码学生产级 Agent 架构

> 本项目的核心用途。面向 Agent 工程师 & RAG 开发者，6 课从核心到外围，每课含理论讲解、源码精读、动手实践。

---

### 第一课：Agent Loop — 引擎的心跳

> **比喻**：Agent Loop 就像一颗心脏。每次跳动（iteration）= 一次"思考→行动→观察"循环。但 Claude Code 的心脏不是简单的 `while(true)`，它更像一颗装了 7 个旁路瓣膜的心脏——遇到不同的"血栓"（错误），不同的瓣膜会打开，把血液（对话）引导回正确的流向，而不是让心脏停跳。

**学什么**：理解生产级 agent loop 如何处理"正常流程之外的一切"。

#### 源码精读

主文件：`src/query.ts`（~70KB，整个项目的大脑）

| 位置 | 要点 | 为什么重要 |
|------|------|-----------|
| L289 | 注释声明 "7 continue sites" | 整个循环的架构蓝图 |
| L307 | `while (true) {` | 循环入口——注意状态是每轮重建的，不是全局 mutable |
| L308-321 | 状态解构 | 每个 iteration 开头从 `state` 对象解构所有状态，实现"纯函数式循环" |
| L554-556 | `stop_reason === 'tool_use' is unreliable` | 关键注释：不依赖 API 的 stop_reason，而是自己检测 tool_use block |
| L659 | `for await (const message of deps.callModel(...))` | 流式 API 调用，边接收边检测工具调用 |
| L829-835 | tool_use block 检测 | 在流式过程中累积工具调用，设置 `needsFollowUp = true` |
| L788-822 | "Withheld errors" 模式 | 可恢复错误不暴露给用户，静默重试——这是 UX 的关键设计 |

**7 个 Continue Sites（旁路瓣膜）**：

| # | 行号 | 触发条件 | 比喻 |
|---|------|---------|------|
| 1 | L950 | 模型降级回退 | 主引擎熄火，切换到备用引擎 |
| 2 | L1115 | Context Collapse 排水 | 水管堵了，先放掉积水再重试 |
| 3 | L1165 | 反应式压缩 | 行李超重，紧急丢掉旧行李 |
| 4 | L1220 | 输出 token 升级（8K→64K） | 发现纸不够大，换张大纸继续画 |
| 5 | L1251 | 输出 token 恢复 | 画到纸边了，贴一张新纸接着画 |
| 6 | L1305 | Stop Hook 阻塞 | 门卫拦住了，注入说明再试 |
| 7 | L1340 | Token 预算续行 | 油箱还有油，继续跑 |

**退出条件**（心脏停跳的 10 种方式）：

| 行号 | reason | 含义 |
|------|--------|------|
| L1357 | `completed` | 正常完成（最常见） |
| L1711 | `max_turns` | 达到最大轮数 |
| L996 | `model_error` | API 错误，不可恢复 |
| L1175 | `prompt_too_long` | 压缩后仍超长 |
| L1051 | `aborted_streaming` | 用户中断 |
| L1520 | `hook_stopped` | Hook 阻止继续 |

#### 动手实践

用 Python/TypeScript 实现一个 **mini agent loop**，要求：
1. `while(true)` + 状态对象重建（不用全局变量）
2. 至少实现 2 个 continue site：模型降级回退 + 输出 token 恢复
3. 至少实现 3 个退出条件：completed / max_turns / model_error
4. 工具调用检测要在流式响应中做，不依赖 `stop_reason`

#### 文档记录

输出一份笔记，回答以下问题：
- 为什么用状态对象重建而不是全局 mutable 变量？（提示：可预测性 + 调试）
- "Withheld errors" 模式对用户体验的影响是什么？
- 7 个 continue site 的设计如何避免了深层 try-catch 嵌套？

---

### 第二课：上下文管理 — 大脑的遗忘艺术

> **比喻**：人脑不会记住每个细节，而是选择性遗忘，把重要的事刻成长期记忆。Claude Code 的上下文管理系统就是它的"遗忘系统"——不是随机丢弃，而是像图书馆管理员一样，按规则把不常用的书搬到地下室（压缩），把烂掉的书扔掉（截断），同时确保正在用的书永远在手边。

**学什么**：Token 是稀缺资源，如何分层管理是 agent 系统的核心工程挑战。

#### 源码精读

核心目录：`src/services/compact/`

| 文件 | 行数 | 角色 |
|------|------|------|
| `compact.ts` | 1705 | 总指挥——全量压缩和分段压缩的调度器 |
| `microCompact.ts` | 531 | 战术级清理——过期工具结果的实时清除 |
| `autoCompact.ts` | 352 | 哨兵——监控 token 水位，触发压缩决策 |
| `apiMicrocompact.ts` | 154 | API 原生压缩——利用 Anthropic SDK 的清理策略 |
| `sessionMemoryCompact.ts` | 368 | 实验性——把对话精华提取成记忆文件 |

**五级压缩策略（从轻到重）**：

```
Level 1: Microcompact（微压缩）
    ↓ 不够？
Level 2: Cached Microcompact（带缓存的微压缩）
    ↓ 不够？
Level 3: Autocompact（自动全量压缩）
    ↓ 不够？
Level 4: Reactive Compact（反应式紧急压缩）
    ↓ 不够？
Level 5: PTL Truncation（直接截断最老的消息）
```

**关键阈值**（`autoCompact.ts`）：

| 常量 | 值 | 含义 |
|------|-----|------|
| `AUTOCOMPACT_BUFFER_TOKENS` (L62) | 13,000 | 剩余 token < 13K 时触发自动压缩 |
| `WARNING_THRESHOLD_BUFFER_TOKENS` (L63) | 20,000 | 剩余 < 20K 时显示警告 |
| `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES` (L70) | 3 | 连续失败 3 次后熔断，不再尝试 |
| `POST_COMPACT_TOKEN_BUDGET` (L123, compact.ts) | 50,000 | 压缩后用于恢复关键文件的 token 预算 |

**可被微压缩清除的工具**（`microCompact.ts` L41-50）：FILE_READ, SHELL, GREP, GLOB, WEB_SEARCH, WEB_FETCH, FILE_EDIT, FILE_WRITE

**对 RAG 开发者的关键洞察**（`microCompact.ts` L36）：
```typescript
const TIME_BASED_MC_CLEARED_MESSAGE = '[Old tool result content cleared]'
```
旧工具结果不是删除，而是替换为占位符——保留了"发生过什么"的语义信息，但丢弃了体积庞大的内容。这和 RAG 中的摘要替换策略异曲同工。

#### 动手实践

设计一个 **上下文管理器**（可用在你自己的 agent 或 RAG 系统中）：
1. 实现 token 水位监控（模仿 `calculateTokenWarningState`）
2. 实现至少 2 级压缩：工具结果清理（Level 1）+ 对话摘要（Level 3）
3. 实现熔断器（连续 N 次压缩失败后停止尝试）
4. 关键：压缩后用占位符替换，不要直接删除

#### 文档记录

- 画出五级压缩的决策流程图
- 对比 Claude Code 的"工具结果清理"和传统 RAG 的"chunk 淘汰"——哪些思路可以互相借鉴？
- 为什么需要熔断器？没有它会发生什么？（提示：无限压缩-调用-再压缩循环）

---

### 第三课：多 Agent 协作 — 分身术

> **比喻**：想象你是一个项目经理，手头任务太多。你可以"分身"——派出几个克隆体。Fork 模式就像把你的记忆完整复制给克隆体（它知道你之前所有的对话），Fresh 模式就像招了个新人（只给他看最近几条消息的简报）。关键问题：怎么让克隆体共享你的"会员卡"（prompt cache），不用每个人都重新办一张？

**学什么**：多 agent 编排不只是"启动多个 agent"，核心挑战是上下文共享和成本控制。

#### 源码精读

| 文件 | 要点 |
|------|------|
| `src/tools/AgentTool/forkSubagent.ts` (211行) | Fork 模式的实现——子 agent 继承父亲的完整上下文 |
| `src/tools/AgentTool/runAgent.ts` (855行) | Agent 生命周期管理——从创建到销毁 |
| `src/utils/forkedAgent.ts` | 上下文继承的工具函数 |

**Fork vs Fresh 的核心区别**（`forkSubagent.ts`）：

```
Fork 模式（L60-71）：
  tools: ['*']           → 继承父亲所有工具
  model: 'inherit'       → 用同一个模型
  permissionMode: 'bubble' → 权限请求冒泡到父亲的 UI
  getSystemPrompt: () => '' → 不生成新 prompt，用父亲的

Fresh 模式（runAgent.ts L370-376）：
  parentMessages.slice(-N) → 只继承最近 N 条消息
  独立 system prompt      → 重新生成
  独立权限上下文           → 自己管理
```

**Prompt Cache 共享的秘密**（`forkSubagent.ts` L107-168）：

`buildForkedMessages()` 函数的精妙之处：
- L113-120：克隆父亲的 assistant message，**保留所有 tool_use block**（包括 thinking、text）
- L142-151：为每个 tool_use 生成**相同的占位符 result**：`'Fork started — processing in background'`
- L158-166：只在最后追加不同的 directive

结果：所有 fork 子 agent 的请求前缀完全一致（父上下文 + 占位符），只有最后一个 text block 不同。API 层面，前缀命中同一个 prompt cache，**多个子 agent 共享同一份缓存费用**。

**编排逻辑写在 prompt 里，不在代码里**：
`src/constants/prompts.ts` 中的多 agent 指令不是用 if-else 实现的，而是自然语言：
- *"Do not rubber-stamp weak work"*
- *"Never hand off understanding to another worker"*
- *"You must understand findings before directing follow-up work"*

好处：更新编排行为不需要重新部署代码。

#### 动手实践

实现一个 **mini multi-agent 框架**：
1. 支持 Fork 和 Fresh 两种模式
2. Fork 模式下，确保所有子 agent 的 API 请求前缀字节一致（实现 cache 共享）
3. 实现 `permissionMode: 'bubble'`——子 agent 的权限请求冒泡到主进程
4. 编排逻辑用 system prompt 实现，不用代码分支

#### 文档记录

- Fork vs Fresh 在哪些场景下各自更合适？（提示：任务相关性 vs 上下文隔离）
- Prompt cache 共享能节省多少成本？估算一下（假设 200K 上下文、5 个子 agent）
- "编排逻辑写在 prompt 里"有什么风险？（提示：prompt injection、可审计性）

---

### 第四课：权限系统 — 看门人的七道关卡

> **比喻**：想象一栋大楼的安保系统。不是只在门口放一个保安，而是设了 7 道关卡：X 光安检（输入验证）→ 门禁卡（工具自检）→ 访客登记（权限上下文）→ 保安呼叫（hooks）→ AI 门禁（分类器）→ 前台确认（用户弹窗）→ 放行执行。而且有个铁律：**deny 永远优先**——即使你是楼主，带着违禁品也不让进。

**学什么**：生产级 agent 的安全架构，如何在"让 AI 做事"和"不让 AI 搞砸"之间找到平衡。

#### 源码精读

| 文件 | 角色 |
|------|------|
| `src/services/tools/toolExecution.ts` (1300行) | 权限检查 + 工具执行的主流程 |
| `src/hooks/toolPermission/PermissionContext.ts` (348行) | 权限决策的上下文构建 |
| `src/Tool.ts` L362-695 | Tool 接口中的权限相关定义 |

**七步权限流水线**（`toolExecution.ts`）：

```
Step 1 (L614-680): 输入验证
  → Zod schema 解析 + validateInput()
  → 格式错就直接拦截，不进后续流程

Step 2 (L700-750): 工具自检
  → 每个工具自带 checkPermissions()
  → 比如 BashTool 检查命令是否安全

Step 3 (PermissionContext.ts L96-348): 构建权限上下文
  → 包装出 logDecision / tryClassifier / runHooks / buildAllow / buildDeny

Step 4 (PermissionContext.ts L216-262): Permission Hooks
  → 外部 hooks 可以拦截或放行
  → hook 的 deny 是终局判决

Step 5 (PermissionContext.ts L174-214): 分类器自动审批
  → 仅 Bash 工具 + feature('BASH_CLASSIFIER') 启用
  → 模糊 case 交给 AI 分类器判断

Step 6 (toolExecution.ts queue): 用户确认弹窗
  → 推入 UI 队列，等用户点允许/拒绝

Step 7 (toolExecution.ts L926-1200): 执行
  → Pre-tool hooks → tool.call() → Post-tool hooks
```

**关键设计决策**（`Tool.ts` L757-792）：
```typescript
const TOOL_DEFAULTS = {
  isConcurrencySafe: () => false,  // 默认不安全——fail-closed
  isReadOnly: () => false,         // 默认假设有写操作
  isDestructive: () => false,
}
```
**Fail-closed 原则**：不确定的一律当危险处理。这和安全领域的"最小权限原则"完全一致。

#### 动手实践

为你自己的 agent 系统设计一个权限中间件：
1. 实现至少 3 层检查：输入验证 → 工具自检 → 用户确认
2. Deny 永远优先（即使在 bypass 模式下，安全路径仍强制检查）
3. Fail-closed 默认值：`isConcurrencySafe=false, isReadOnly=false`
4. 实现 pre/post hooks 机制

#### 文档记录

- Fail-closed vs Fail-open 在 agent 系统中的 trade-off
- 为什么分类器只用于 Bash 工具？（提示：Bash 是最危险也最灵活的工具）
- 7 步流水线 vs 单一权限检查，在延迟和安全性上的 trade-off

---

### 第五课：System Prompt 工程 — 缓存感知的咒语书

> **比喻**：System prompt 就像一本咒语书。普通人把所有咒语写在一页纸上，每次施法都要从头念。Claude Code 的做法是把咒语书分成"固定篇"和"变化篇"——固定篇（你是谁、怎么用工具）刻在石板上（prompt cache），只有变化篇（用户语言、MCP 配置）每次重新写。这样每次施法，70% 的咒语直接从石板读取，只需要念剩下的 30%。

**学什么**：Prompt 不只是"写好"就行，还要为 cache 优化结构——这直接影响成本和延迟。

#### 源码精读

主文件：`src/constants/prompts.ts`（1000+ 行）

**缓存分割边界**（L114-115）：
```typescript
export const SYSTEM_PROMPT_DYNAMIC_BOUNDARY = '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'
```

**分段结构**：
```
[Static — cacheable, scope: 'global']
  ├─ getSimpleIntroSection()      (L175-184)  → 角色定义、安全指令
  ├─ getSimpleSystemSection()     (L186-196)  → 工具执行模型、压缩提示
  ├─ getSimpleDoingTasksSection() (L199-243)  → 代码风格、任务方法论
  ├─ getHooksSection()            (L127-129)  → Hook 行为定义
  └─ getSystemRemindersSection()  (L131-133)  → 系统提醒标签解释

── DYNAMIC_BOUNDARY ──

[Dynamic — per-user/session, scope: 'user' or 'session']
  ├─ getLanguageSection()         (L142-149)  → 语言偏好
  ├─ getOutputStyleSection()      (L151-158)  → 输出格式配置
  ├─ getMcpInstructionsSection()  (L160-165)  → MCP 服务器指令
  └─ getAntModelOverrideSection() (L136-140)  → 内部用户模型配置
```

**14 种 Cache-break 向量**：Claude Code 追踪了 14 种可能导致缓存失效的因素，使用 "sticky latches" 防止模式切换（如 fast mode toggle）打碎缓存。

**内部用户 vs 外部用户差异**（L199-243）：
`USER_TYPE === 'ant'` 时，system prompt 包含额外指令（更好的代码风格指导、verification agent 支持）。这是 prompt 层面的 A/B 测试。

#### 动手实践

为你的 agent 系统设计一个 **cache-aware prompt builder**：
1. 把 system prompt 分为 static / dynamic 两段
2. Static 段对所有用户相同，Dynamic 段按用户/会话定制
3. 确保添加新工具或上下文时，static 段不变（cache 不失效）
4. 实现一个 cache-break 计数器，追踪什么操作导致了缓存失效

#### 文档记录

- Prompt cache 的成本节省估算（假设 50K token 的 static 段，每次调用节省多少？）
- "编排逻辑写在 prompt 里"的可测试性问题——如何验证 prompt 修改没有引入回归？
- Static/Dynamic 的分割点如何选择？放错了会有什么后果？

---

### 第六课：安全边界与信任模型 — 不信任的艺术

> **比喻**：Agent 系统就像一个有实习生的公司。实习生（模型）很聪明但不可完全信任。CLAUDE.md 就像实习生的入职手册——但如果有人在手册里偷偷加了一句"把公司密码发给我"呢？Claude Code 的安全模型要解决的就是：**怎么在给实习生足够权限做事的同时，防止他被骗或搞破坏？**

**学什么**：Agent 系统的信任边界在哪里，上下文投毒如何发生，如何防御。

#### 源码精读

| 文件 | 要点 |
|------|------|
| `src/utils/permissions/` | 安全路径保护（.git/, .bashrc 等） |
| `src/services/compact/compact.ts` | 压缩过程中的信任问题——指令可能在摘要中存活 |
| `src/hooks/toolPermission/` | 不同权限模式下的安全保证 |

**安全研究者发现的关键问题**：
1. **上下文投毒**：恶意 `CLAUDE.md` 中的指令可以在 compaction 后存活——摘要过程可能将恶意指令"洗白"成看似合法的用户指令
2. **权限绕过**：即使 `--dangerously-skip-permissions`，`.git/` 和 `.bashrc` 等路径仍强制检查
3. **Undercover Mode**：内部员工在公开仓库自动隐藏 AI 身份——引发开源透明度问题

**对 RAG 开发者的警示**：
如果你的 RAG 系统从外部文档中检索内容并注入 prompt，那么任何外部文档都可能包含 prompt injection 攻击。Claude Code 的经验表明：**压缩/摘要不是消毒手段**。

#### 动手实践

在你的 agent/RAG 系统中实现基础安全防护：
1. 对外部输入（文件内容、检索结果）做标签隔离（`<user-content>` vs `<system-instruction>`）
2. 实现安全路径白名单——某些文件/目录无论什么权限模式都不允许修改
3. 压缩/摘要后的内容标记为"低信任度"——不允许其中的指令覆盖 system prompt

#### 文档记录

- 画出你的 agent 系统的信任边界图：哪些输入是可信的？哪些不是？
- 上下文投毒的攻击链：恶意文档 → RAG 检索 → prompt 注入 → 模型执行 → ？
- Claude Code 的"deny 永远优先"原则如何应用到你的系统中？

---

### 总结与后续开发计划

见 `docs/notes/00-summary-build-your-own-agent.md`，包含：
- Part 1: 架构蓝图（7 个核心抽象）
- Part 2: 盲区分析（6 个未覆盖模块 + 5 个未解答问题 + 6 课未做实践）
- Part 3: 开发路线图（5 个 Phase，从最小 agent 到成本优化）
- Part 4: 关键决策清单（6 个编码前必须确定的选型）

### 学习进度跟踪

| 课程 | 状态 | 笔记位置 |
|------|------|---------|
| 第一课：Agent Loop | ✅ 源码精读+笔记完成，剩实践 | `docs/notes/01-agent-loop.md` |
| 第二课：上下文管理 | ✅ 源码精读+笔记完成，剩实践 | `docs/notes/02-context-management.md` |
| 第三课：多 Agent 协作 | ✅ 源码精读+笔记完成，剩实践 | `docs/notes/03-multi-agent.md` |
| 第四课：权限系统 | ✅ 源码精读+笔记完成，剩实践 | `docs/notes/04-permissions.md` |
| 第五课：System Prompt | ✅ 源码精读+笔记完成，剩实践 | `docs/notes/05-system-prompt.md` |
| 第六课：安全边界 | ✅ 源码精读+笔记完成，剩实践 | `docs/notes/06-security.md` |

### 参考资料

- [Claude Code Agent Harness Architecture - WaveSpeedAI](https://wavespeed.ai/blog/posts/claude-code-agent-harness-architecture/)
- [10 Agentic AI Harness Patterns - Ken Huang](https://kenhuangus.substack.com/p/the-claude-code-leak-10-agentic-ai)
- [5 个 Agent 设计模式拆解 - 腾讯云](https://cloud.tencent.com/developer/article/2649112)
- [learn-claude-code - shareAI-lab](https://github.com/shareAI-lab/learn-claude-code)
- [Claude Code 源码深度解析 - 知乎](https://zhuanlan.zhihu.com/p/2022442135182406883)
- [不会做 RAG 都来学 Claude Code - Zilliz](https://zilliz.com.cn/blog/Learn-Claude-Code-for-local-RAG-and-agents)

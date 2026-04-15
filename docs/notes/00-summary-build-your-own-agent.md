# 构建生产级 Agent 框架：架构设计与开发计划

## Context

通过 6 课对 Claude Code v2.1.88 源码的系统精读，我们已经提炼出生产级 Agent 系统的核心设计模式。本计划将这些模式整合为一份可直接指导开发的架构文档，同时识别当前学习中的盲区和后续需要补充的内容。

> **版本说明**：本文档经过红蓝对抗评审（红队 14 项发现，蓝队裁决 4 Accept / 7 Partial / 3 Reject），下文已整合评审结论。

---

## Part 0: 五条不可违反的铁律

无论你在哪个 Phase，以下原则从第一行代码起就必须遵守：

| # | 铁律 | 违反后果 | 来源 |
|---|------|---------|------|
| 1 | **用 content block 检测 tool_use，永远不依赖 stop_reason** | 工具调用被漏检，agent 提前终止 | 第一课：L554 `stop_reason is unreliable` |
| 2 | **工具结果用占位符替换，不要删除** | 破坏 tool_use/tool_result 的一一对应关系，API 报错 | 第二课：`[Old tool result content cleared]` |
| 3 | **Static prompt 段禁止任何条件逻辑** | N 个条件 → 2^N 种缓存变体，命中率从 100% 暴跌到 100%/2^N | 第五课：2^N 碎片化问题 |
| 4 | **Deny 规则永远不降级为 Ask** | 攻击者可构造"无法解析"的命令绕过安全策略 | 第四课：`don't downgrade deny to ask` |
| 5 | **压缩/摘要不是消毒手段** | 恶意指令在 compaction 中被"洗白"为合法用户意图 | 第六课：上下文投毒攻击链 |

---

## Part 1: 架构蓝图

### 1.1 核心抽象：三个循环

生产级 Agent 不是一个循环，而是三个嵌套循环：

```
外循环：Session（会话生命周期）
  │  负责：记忆加载、上下文恢复、会话持久化
  │
  ├─ 中循环：Turn（一次用户交互）
  │    │  负责：prompt 构建、上下文压缩、模型调用
  │    │
  │    └─ 内循环：Tool Execution（工具执行批次）
  │         负责：权限检查、并发执行、结果收集
```

Claude Code 的 `query.ts` 实际上是一个统一的 `while(true)` 状态机（7 个 continue site，12 个退出条件）——中循环和内循环并没有分离，因为恢复型 continue site（如 collapse drain、reactive compact）需要跳过工具执行直接回到 API 调用。分离三层在概念上更清晰，但实现时要注意：**恢复逻辑会模糊层级边界**。如果你选择分离，需要设计跨层的恢复信号机制。

### 1.2 状态模型

```
State {
  messages: Message[]              // 不可变，每轮重建
  turn_count: int                  // 当前轮数
  compact_tracking: {              // 压缩状态
    compacted: bool
    consecutive_failures: int      // 熔断器计数
  }
  recovery: {                      // 恢复状态
    max_output_retries: int        // 输出 token 恢复次数
    has_attempted_compact: bool    // 是否已尝试压缩
  }
  transition: {                    // 调试用：为什么进入这一轮
    reason: string
  }
}
```

铁律：每个 continue site 构造完整的新 State 对象，不修改原对象。

### 1.3 工具接口

```
Tool {
  name: string
  description: string -> string
  input_schema: JSONSchema
  
  execute(input, context) -> Result
  check_permissions(input, context) -> allow | deny | ask
  validate_input(input) -> ok | error
  
  // Fail-closed 默认值（注意：是方法不是静态字段）
  is_concurrency_safe(input) -> bool  // 默认 false；可能依赖输入（如两个读不同文件=安全）
  is_read_only(input) -> bool         // 默认 false
  is_destructive(input) -> bool       // 标记不可逆操作
  is_enabled() -> bool                // 动态启用/禁用
}
```

> **红队发现**：Claude Code 的完整 Tool 类型有 40+ 个字段/方法（含 UI 渲染、搜索索引、权限匹配器等）。上面是最小实现集。`is_concurrency_safe` 和 `is_read_only` 是**方法**（接收 input），不是静态布尔值——并发安全性可能取决于具体输入（如两个文件读取目标不同时安全，目标相同时不安全）。

```
// Claude Code 完整 Tool 类型中值得后续添加的字段：
prompt(options) -> string              // 动态 prompt（不只是 description）
max_result_size_chars: int             // 超过此长度的结果持久化到磁盘
backfill_observable_input(input)       // 为观察者（hooks/日志）补充派生字段
interrupt_behavior() -> 'cancel'|'block'  // 用户中断时的行为
```

### 1.4 Prompt 结构

```
SystemPrompt = [
  ...static_sections,        // 所有用户共享，可全局缓存
  DYNAMIC_BOUNDARY,
  ...dynamic_sections,        // 每用户/每会话不同，不缓存
]
```

禁止在 static_sections 中放任何条件逻辑（N 个条件 → 2^N 种缓存前缀变体）。对于必须每轮重算的 dynamic section，用 `DANGEROUS_uncached` 前缀标记并强制写明理由，防止随手打碎缓存。

### 1.5 权限流水线

```
输入验证 → 工具自检 → 内部字段清理 → PreToolUse Hooks → Deny 规则 → Allow 规则 → 分类器 → 用户确认 → 执行 → PostToolUse Hooks
│                      │                │                                                                           │
│  Phase 1 实现        │  Phase 3 实现                                                                              │  Phase 3 实现
（任何一步 deny 即终止，deny 永远优先于 allow）
```

> **注意**：Claude Code 的分类器在输入验证后就**投机性启动**（与后续步骤并行运行），到需要结论时大概率已完成。此外 PostToolUse Hooks 可以阻止后续循环继续。

### 1.6 多 Agent 模型

```
Fork: 继承完整上下文 + 共享 prompt cache（字节一致前缀，成本差异可达数千倍）
Fresh: 独立上下文 + 精简 prompt（去掉不需要的规则，节省 ~5-15 Gtok/week）
Team: 命名 agent + 显式 SendMessage 通信（文本输出不可见）
```

> **Fork 的关键约束**：传父亲的渲染后 system prompt 字节（不重新生成），保留完整工具池（包括 AgentTool），所有 tool_result 用同一占位符文本，thinking block 原样保留。防递归用运行时消息标记检测（而非移除 AgentTool），原因是移除工具会改变 tool_defs 字节导致缓存失效。

```
```

### 1.7 安全信任模型

```
硬编码层（不可被 prompt 覆盖）> 可配置层 > 外部输入（不可信）
```

---

## Part 2: 当前盲区分析

六课覆盖了 Claude Code 的核心架构，但以下领域**没有深入展开**，后续需要补充：

### 2.1 未覆盖的源码模块

| 模块 | 文件 | 为什么重要 |
|------|------|-----------|
| **流式工具执行器** | `src/services/tools/StreamingToolExecutor.ts` | 理解并发控制：哪些工具可以并行、如何处理并发冲突 |
| **API 重试与回退** | `src/services/api/withRetry.ts` | 指数退避、fallback model 切换、错误分类（可重试 vs 不可重试） |
| **MCP 集成** | `src/services/mcp/` | Model Context Protocol 如何扩展工具生态——你的 agent 如何接入外部工具 |
| **记忆系统** | `src/utils/memory/` | MEMORY.md 索引 + frontmatter 主题文件的完整实现 |
| **Session 持久化** | `src/utils/sessionStorage.ts` | 会话如何序列化/恢复——对长时间运行的 agent 至关重要 |
| **Token 估算** | `src/services/tokenEstimation.ts` | 客户端 token 估算的实现——不调 API 如何估算 token 数 |

### 2.2 未深入的设计问题

| 问题 | 相关课程 | 需要进一步研究 |
|------|---------|--------------|
| 压缩 prompt 的 9 节模板是否适合所有场景？ | 第二课 | 对比不同结构化摘要模板的效果 |
| Fork 子 agent 的 maxTurns=200 是否合理？ | 第三课 | 子 agent 的资源消耗上限如何设定 |
| 分类器的训练数据和 prompt 是什么？ | 第四课 | `classifyBashCommand` 的内部实现在 feature gate 后面，无法看到 |
| Prompt cache 的实际命中率是多少？ | 第五课 | 需要实际部署后用遥测数据验证 |
| CLAUDE.md 投毒的实际防御效果如何？ | 第六课 | 需要红队测试验证 |

### 2.3 未实践的动手环节

六课都完成了源码精读和笔记，但**动手实践全部未做**。每课的实践任务：

| 课程 | 实践任务 | 产出物 |
|------|---------|--------|
| 1 | 实现 mini agent loop（2 个 continue site + 3 个退出条件） | `practice/01-agent-loop/` |
| 2 | 实现上下文管理器（2 级压缩 + 熔断器） | `practice/02-context-manager/` |
| 3 | 实现 mini multi-agent（Fork + Fresh + cache 共享） | `practice/03-multi-agent/` |
| 4 | 实现权限中间件（3 层检查 + fail-closed） | `practice/04-permissions/` |
| 5 | 实现 cache-aware prompt builder | `practice/05-prompt-builder/` |
| 6 | 实现安全防护（标签隔离 + 安全路径白名单） | `practice/06-security/` |

---

## Part 3: 开发路线图

### Phase 1: 骨架（目标：能跑起来的最小 agent）

**核心文件**：
- `agent/loop.py` — Agent Loop（while true + 状态重建）
- `agent/tools.py` — Tool 接口 + 3 个基础工具（读文件、搜索、执行命令）
- `agent/prompt.py` — Prompt Builder（Static/Dynamic 分段）
- `agent/main.py` — 入口：接收用户输入 → 调用 loop → 输出结果

**验收标准**：
- 能完成"读取某文件并回答问题"的端到端流程
- 工具调用检测基于 content block，不依赖 stop_reason
- 状态每轮重建（immutable）
- API 错误被捕获并优雅提示（不崩溃），即使不做复杂恢复

> **Phase 1 使用非流式（batch）API 调用**。此模式下 content block 检测是简单的数组遍历。流式模式下检测变得复杂（边接收边检测），推迟到 Phase 5。

### Phase 2: 韧性（目标：长对话不崩溃）

**核心文件**：
- `agent/context.py` — 上下文管理器（微压缩 + 全量压缩 + 熔断器）
- `agent/recovery.py` — 分级恢复（输出 token 升级 → 恢复消息 → 放弃）
- `agent/errors.py` — Withheld Errors（可恢复错误静默处理）

**验收标准**：
- 对话超过上下文窗口时自动压缩，不报错
- 微压缩使用占位符替换（如 `[Old tool result content cleared]`），不直接删除消息
- 压缩连续失败 3 次后熔断
- 可恢复错误不暴露给用户

> **安全警告**：压缩系统必须把摘要后的内容视为与原始内容同等信任级别。恶意指令（如来自外部文件的 prompt injection）可以在 compaction 过程中被"洗白"——被摘要为看似合法的用户意图。**压缩不是消毒。**

### Phase 3: 安全（目标：不被滥用）

**核心文件**：
- `agent/permissions.py` — 权限流水线（deny 优先 + fail-closed）
- `agent/security.py` — 命令安全检查（危险命令黑名单）
- `agent/trust.py` — 信任边界（外部输入标记 + 标签防伪）

**验收标准**：
- `rm -rf /` 被拦截
- deny 规则不因命令无法解析而降级
- 外部内容被显式标记为不可信
- 配置文件（如 CLAUDE.md 等效物）按四级信任层次加载：系统 > 用户 > 项目（⚠️ 仓库签入）> 本地

### Phase 4: 持久化与可观测（目标：能恢复、能排查）

**核心文件**：
- `agent/session.py` — Session 持久化（序列化/恢复对话历史，进程崩溃后可续接）
- `agent/logging_.py` — 结构化日志（JSON 格式，含 request_id / turn / tool_name / latency）
- `agent/config.py` — 配置管理（YAML/TOML 配置文件 + 环境变量覆盖 + 运行时 reload）

**验收标准**：
- `kill -9` 后重启，能从上次对话继续（不丢超过 1 轮）
- 每次工具调用产出一条结构化日志，包含 tool_name、耗时、is_error、token 消耗
- 配置不硬编码：model、max_turns、压缩阈值等全部可配置，修改配置不改代码
- 日志可被 grep/jq 查询（排查问题不需要读代码）

> **CC 对应**：`sessionStorage.ts`（会话序列化）、telemetry 模块（结构化事件）、多层 config 加载（CLI flags > env > config file > defaults）。

### Phase 5: 健壮执行（目标：真实网络环境下不脆断）

**核心文件**：
- `agent/retry.py` — 分级重试（指数退避 + 429 retry-after 遵循 + 错误分类矩阵）
- `agent/executor.py` — 并发工具执行器（并行调度 + sibling abort + 有序结果收集）
- `agent/tokens.py` — 客户端 Token 估算（不调 API 预判是否超窗口，决定是否预压缩）
- `agent/shutdown.py` — 优雅关闭（Ctrl+C 时等待当前工具完成、清理临时文件、flush 日志）

**验收标准**：
- 429 响应自动遵循 `retry-after` 头，不盲目重试
- 连续 5xx 触发指数退避（1s → 2s → 4s → ...），上限 60s
- 3 个独立的 read_file 调用能并行执行，总耗时 ≈ 单次而非 3 倍
- 并行工具中一个超时不阻塞其他工具（sibling abort）
- Ctrl+C 时不留孤儿进程、不丢未 flush 的日志

> **CC 对应**：`withRetry.ts`（800+ 行的重试系统，远比简单 wrapper 复杂）、`StreamingToolExecutor.ts`（并发执行 + 有序输出）、`tokenEstimation.ts`。红队审查 #5 #6 明确标注这两个是盲区。

### Phase 6: 扩展性（目标：能力不硬编码）

**核心文件**：
- `agent/memory.py` — 记忆系统（索引文件 + 主题文件，跨会话持久化用户偏好和项目上下文）
- `agent/hooks.py` — Hook 系统（PreToolUse / PostToolUse / Stop 三类钩子，外部脚本可拦截/修改工具行为）
- `agent/mcp_client.py` — MCP 客户端（Model Context Protocol，动态发现并接入外部工具服务器）

**验收标准**：
- 用户说"记住我偏好简洁输出"后，下次新会话自动生效
- 外部 hook 脚本能拦截 BashTool 的 `rm` 命令（不改 agent 代码）
- 通过 MCP 接入一个外部工具服务器（如文件系统 MCP），agent 能自动发现并使用其工具
- 新增工具不需要改 loop.py 或 main.py（工具注册是声明式的）

> **CC 对应**：`src/utils/memory/`（MEMORY.md 索引 + frontmatter 主题文件）、`src/hooks/`（三类 hook + 外部脚本执行）、`src/services/mcp/`（MCP 客户端 + 工具动态注册）。这三个模块在六课精读中未深入，需要补充源码阅读。

---

### Phase 7: 多 Agent（目标：分身协作）⚡ 降级：面试讲解优先，实现可选

**核心文件**：
- `agent/fork.py` — Fork 子 agent（缓存一致前缀）
- `agent/fresh.py` — Fresh 子 agent（精简上下文）
- `agent/messaging.py` — 显式通信协议

**验收标准**：
- Fork 子 agent 的 API 请求前缀与父 agent 字节一致（前缀不一致时成本差数千倍）
- Fresh 子 agent 去掉不需要的规则上下文
- 子 agent 无法递归 fork（用运行时消息标记检测，不移除工具——移除会破坏缓存一致性）

> **降级理由**：Fork cache 共享的原理（字节一致前缀 → 共享 prompt cache）面试用嘴讲比写出来更有说服力。除非目标岗位明确要求多 agent 编排经验，否则 Phase 6 完成即可。

### Phase 8: 流式与成本优化（目标：成本可控）⚡ 降级：面试讲解优先，实现可选

**核心文件**：
- `agent/cache.py` — Prompt cache 监控（碎片化检测）
- `agent/streaming.py` — 流式 API 调用（边接收边检测 tool_use，替代 batch 模式）
- `agent/metrics.py` — 成本追踪（token 使用、缓存命中率、每次调用费用明细）

**验收标准**：
- Prompt cache 命中率 > 80%（通过 API 响应中的 `cache_read_input_tokens` / `cache_creation_input_tokens` 字段测量）
- 流式模式下工具检测在流完成前就开始（TTFT 优化）
- 能输出每次调用的成本明细（含 prompt token、completion token、缓存命中/未命中）

> **降级理由**：Phase 1 已实现 Static/Dynamic 分段和 cache_control 断点，基础缓存架构已到位。流式执行是性能优化，不是功能缺失。

---

## Part 4: 需要做出的关键决策

在开始编码前，需要确定：

| 决策 | 选项 | Claude Code 的选择 | 建议 |
|------|------|-------------------|------|
| 实现语言 | Python / TypeScript / Rust | TypeScript | 取决于你的技术栈和部署场景 |
| LLM 提供商 | Anthropic / OpenAI / 多家 | Anthropic only | 建议先单家，稳定后抽象多家 |
| 检索方式 | RAG / 工具化检索 / 混合 | 工具化检索（Grep） | 代码场景用工具化，文档场景考虑混合 |
| 上下文窗口策略 | 固定 / 动态 / 混合 | 动态（五级压缩） | 从两级压缩开始，逐步加层 |
| 权限模型 | 全自动 / 全手动 / 分级 | 分级（规则+分类器+弹窗） | 从规则开始，后加分类器 |
| 部署形态 | CLI / API / Web | CLI | 取决于用户场景 |

---

## Appendix: 红蓝对抗评审记录

### 红队发现（14 项）

| # | 发现 | 严重度 | 蓝队裁决 |
|---|------|--------|---------|
| 1 | "三个循环"抽象不匹配实际的统一状态机 | HIGH | **PARTIAL** — 作为架构目标更清晰，但需注明恢复逻辑会模糊层级边界 |
| 2 | State 模型缺少 toolUseContext 等 5+ 关键字段 | CRITICAL | **PARTIAL** — Phase 1 简化合理，已注明是最小起点 |
| 3 | Tool 接口缺少 30+ 方法；静态字段 vs 方法混淆 | CRITICAL | **ACCEPT** — 已修正为方法签名，补充完整类型参考 |
| 4 | 上下文管理有 6 层不是 2 层 | HIGH | **PARTIAL** — 两层起步合理，但已补充生产系统的完整分层 |
| 5 | 重试逻辑是 800+ 行的复杂系统，不是简单包装 | HIGH | **PARTIAL** — 已列为盲区，Phase 2 需要比计划更深的投入 |
| 6 | StreamingToolExecutor 有 sibling abort、有序输出等复杂机制 | MEDIUM | **PARTIAL** — 已列为盲区模块 |
| 7 | 权限流水线缺少 backfill、post-tool hooks、denial tracking | MEDIUM | **ACCEPT** — 已补充完整 7+ 步流水线 |
| 8 | Fork 实现缺少防递归机制、字节对齐构造、worktree 隔离 | MEDIUM | **ACCEPT** — 已补充关键约束说明 |
| 9 | Stop Hooks / Post-Sampling Hooks 完全缺失 | HIGH | **ACCEPT** — 已在权限流水线中补充 PostToolUse Hooks |
| 10 | Phase 顺序有依赖问题（安全应在韧性之前？流式不是可选优化？）| MEDIUM | **REJECT** — 学习路线图允许早期阶段只有开发者自己使用 |
| 11 | Prompt 结构缺少 systemContext、appendSystemPrompt、tool prompts | LOW | **PARTIAL** — 生产系统更复杂，但 Phase 1 不需要 |
| 12 | 缺少异步预取基础设施（memory prefetch、skill discovery 等）| MEDIUM | **PARTIAL** — 属于 Phase 5 优化范畴 |
| 13 | "Immutable State" 声明有例外：toolUseContext 在迭代内是 mutable 的 | LOW | **ACCEPT** — 已在 1.1 注明 |
| 14 | Token Budget 系统（continue site + 跨压缩追踪）未提及 | MEDIUM | **PARTIAL** — 属于高级功能，可在 Phase 5 后补充 |

### 蓝队关键判断

- **Phase 顺序已调整**（2026-04-10）：原 5 Phase 路线图目标是"学习路线图"，调整后目标是"生产级 agent"。新增 Phase 4（持久化与可观测）、Phase 5（健壮执行）、Phase 6（扩展性）补齐工程基建缺口。原 Phase 4（多 Agent）和 Phase 5（流式优化）降级为 Phase 7/8，面试讲解优先、实现可选。
- **简化是刻意的**：Phase 1-3 是最小起步，但 Phase 4-6 补齐后整体达到"可部署给真实用户"的标准。
- **三层循环作为架构目标保留**：比 Claude Code 的单循环状态机更清晰，但注明了恢复逻辑会模糊边界。
- **Python 文件名保留**：用户是 Python 技术栈，架构模式是语言无关的。

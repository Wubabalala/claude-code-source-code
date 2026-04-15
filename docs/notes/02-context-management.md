# 第二课：上下文管理 — 大脑的遗忘艺术

> 核心目录：`src/services/compact/`
> 关键文件：`microCompact.ts`（531行）、`autoCompact.ts`（352行）、`compact.ts`（1705行）、`prompt.ts`（375行）

---

## 一、全景：四级压缩流水线

在 `query.ts` 循环的阶段 ① 中，按固定顺序执行：

```
原始对话
  │
  ▼  Snip（剪枝）           粗暴快速，剪掉最老的消息组
  │
  ▼  Microcompact（微压缩）   清除旧工具结果内容，保留消息骨架
  │
  ▼  Context Collapse（折叠） 实验性，合并多段对话为压缩表示
  │
  ▼  Autocompact（全量压缩）  调用 Claude 生成 9 节结构化摘要
  │
  ▼  送入 API
```

如果 API 仍返回 `prompt_too_long`，还有**反应式压缩**（query.ts continue site #2、#3）作为最后防线。

---

## 二、Microcompact — 最精妙的一级

### 核心思想

工具结果是上下文中最大的"一次性消耗品"。文件读取的 5000 行原文已被模型消化，后续轮次中它只占空间不提供新信息。

### 两条路径

**路径 A：Time-based（缓存已冷）**

距离上一次 assistant 消息超过阈值时（缓存已过期），直接替换旧工具结果为占位符：

```typescript
// microCompact.ts L483
return { ...block, content: '[Old tool result content cleared]' }
```

**为什么是替换不是删除？** API 要求 `tool_result` 和 `tool_use` 一一对应。删掉 tool_result 会导致 API 报错。占位符保持消息结构完整性。

保留策略（L461-462）：
```typescript
const keepRecent = Math.max(1, config.keepRecent)  // 至少保留 1 个
```
踩过的坑：`slice(-0)` 等于 `slice(0)` = 整个数组，什么都不清。所以强制 `Math.max(1, ...)`。

**路径 B：Cached Microcompact（缓存还热）**

不修改本地消息，而是通过 API 的 `cache_edits` 告诉服务器"删掉缓存中这些 tool_result 的内容"。

好处：客户端消息不变 → 请求前缀不变 → prompt cache 命中。

约束：只在主线程运行，子 agent 不能用（cachedMCState 是全局的，子 agent 注册的工具 ID 在主线程不存在）。

### 哪些工具的结果可被清除

```typescript
// microCompact.ts L40-49
const COMPACTABLE_TOOLS = new Set([
  FILE_READ, SHELL, GREP, GLOB,
  WEB_SEARCH, WEB_FETCH, FILE_EDIT, FILE_WRITE
])
```

**在列表里的**：原始数据搬运工。结果体积大，但信息已被模型消化。

**不在列表里的**（AgentTool、AskUserQuestion、MCPTool）：精炼信息源。结果本身就是总结性文本，体积小但信息密度高，清除省不了多少 token 反而丢失决策信息。

---

## 三、Autocompact — 调用模型总结自己

### 触发条件

```
当前 token 数 >= 上下文窗口 - 输出预留(20K) - 缓冲区(13K)
```

即上下文占用约 93% 时触发。

### 熔断器（Circuit Breaker）

```typescript
// autoCompact.ts L69
const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
```

来源注释（L67-68）：
> 1,279 sessions had 50+ consecutive failures (up to 3,272), wasting ~250K API calls/day globally.

没有熔断器时，不可恢复地超限的会话每轮都触发注定失败的压缩，全平台每天浪费 25 万次 API 调用。

### 压缩优先级

```typescript
// autoCompact.ts L287-310
// 1. 先尝试 Session Memory 压缩（实验性，更轻量）
const sessionMemoryResult = await trySessionMemoryCompaction(...)
if (sessionMemoryResult) return

// 2. 回退到全量压缩
const compactionResult = await compactConversation(...)
```

---

## 四、压缩 Prompt 的设计（值得反复学习）

`src/services/compact/prompt.ts`

### 整体结构

```
NO_TOOLS_PREAMBLE        ← "不许调工具！调了会被拒绝，你会失败！"
BASE_COMPACT_PROMPT      ← 要求生成 9 节结构化摘要
ANALYSIS_INSTRUCTION     ← 先在 <analysis> 里打草稿
NO_TOOLS_TRAILER         ← 再次提醒"不许调工具"
```

### 9 节摘要结构

1. **用户请求和意图** — 所有显式请求
2. **关键技术概念** — 技术栈、框架、概念
3. **文件和代码片段** — 含完整代码！不只是文件名
4. **错误和修复** — 每个错误的原因 + 修复方式
5. **问题解决** — 已解决和进行中的
6. **所有用户消息** — 非工具结果的用户消息原文列出
7. **待办任务** — 明确被要求做的
8. **当前工作** — 压缩前最后在做什么（精确到文件和代码）
9. **可选下一步** — 只列与用户最近请求直接相关的

### 三个精妙设计

**1. `<analysis>` 标签：一次性质量助推器**

让模型先在 `<analysis>` 里打草稿，再在 `<summary>` 里写正式摘要。`formatCompactSummary()` 直接删掉 `<analysis>`：

```typescript
// prompt.ts L316-318
formattedSummary = formattedSummary.replace(/<analysis>[\s\S]*?<\/analysis>/, '')
```

**为什么删掉？** 两个原因：
- 草稿已完成使命（提升了摘要质量），保留无额外信息价值
- `<analysis>` 通常和 `<summary>` 一样长甚至更长，保留它会让压缩产物大一倍，违背压缩的初衷

**本质**：生成时消耗 API token（付出成本），但不占用后续上下文（节省空间）。

**2. 首尾双重"不许调工具"**

不是修辞强调，是数据驱动的工程措施。`prompt.ts` L12-18 注释：

> on Sonnet 4.6+ the model sometimes attempts a tool call despite the weaker trailer instruction. With maxTurns: 1, a denied tool call means no text output → falls through to streaming fallback (**2.79%** on 4.6 vs 0.01% on 4.5)

压缩用 `maxTurns: 1`，模型如果调工具而不输出文本，这次调用直接废了。首部恐吓 + 尾部兜底，压低 2.79% 的失败率。

**3. 压缩后的续接指令**

```
"Continue without asking questions. Resume directly —
 do not acknowledge the summary, do not recap,
 do not preface with 'I'll continue'.
 Pick up as if the break never happened."
```

防止压缩后模型"醒来"先说一大段废话。

### 压缩后的文件恢复

```typescript
// compact.ts L122-124
POST_COMPACT_MAX_FILES_TO_RESTORE = 5      // 最多恢复 5 个文件
POST_COMPACT_TOKEN_BUDGET = 50_000          // 总预算 50K token
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000    // 每个文件最多 5K token
```

确保模型不会因压缩丢失正在编辑的文件内容。

---

## 五、关键阈值速查表

| 常量 | 值 | 位置 | 含义 |
|------|-----|------|------|
| `AUTOCOMPACT_BUFFER_TOKENS` | 13,000 | autoCompact.ts:61 | 剩余 < 13K 触发自动压缩 |
| `WARNING_THRESHOLD_BUFFER_TOKENS` | 20,000 | autoCompact.ts:62 | 剩余 < 20K 显示警告 |
| `MAX_OUTPUT_TOKENS_FOR_SUMMARY` | 20,000 | autoCompact.ts:29 | 压缩 API 调用的输出预留 |
| `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES` | 3 | autoCompact.ts:69 | 熔断器阈值 |
| `POST_COMPACT_TOKEN_BUDGET` | 50,000 | compact.ts:123 | 压缩后文件恢复的总 token 预算 |
| `POST_COMPACT_MAX_FILES_TO_RESTORE` | 5 | compact.ts:122 | 压缩后最多恢复的文件数 |
| `IMAGE_MAX_TOKEN_SIZE` | 2,000 | microCompact.ts:37 | 图片/文档的固定 token 估算 |

---

## 六、对 RAG 开发者的 Takeaway

### 1. "Grep 优于 RAG" 的工程根据

Claude Code 的"检索"不是 embedding，而是给模型 Grep 工具让它自己搜。配合微压缩：

```
搜索 → 消化 → 清除旧结果 → 腾出空间 → 搜索新内容
```

对比 RAG 的 `预索引→检索→注入`：
- 不需要预处理（建索引）
- 搜索意图由模型实时决定（更精准）
- 上下文空间被动态复用

### 2. 占位符替换策略可直接复用

当 RAG chunk 不再需要时，替换为 `[Content retrieved earlier, now cleared]`，而非删除。模型仍知道"这里检索过什么类型的内容"，但不占 token。保持消息结构完整。

### 3. 熔断器是必须的

任何自动触发的操作（检索、压缩、重试）都必须有熔断器。Claude Code 的教训：没有熔断器 → 边界情况 → 每天浪费 25 万次 API 调用。

### 4. 压缩产物的信息密度要高

9 节结构化摘要 > 自由格式摘要。结构化保证了关键信息（文件名、代码片段、错误修复、用户原话）不会在摘要过程中丢失。`<analysis>` 打草稿进一步提升质量，但草稿本身不保留。

---

## 七、可复用的设计原则

| 原则 | Claude Code 的体现 | 你的系统如何应用 |
|------|-------------------|----------------|
| **替换不删除** | tool_result → 占位符 | RAG chunk 过期后替换为摘要占位符 |
| **分级压缩** | micro → auto → reactive | 轻量清理 → 摘要压缩 → 紧急截断 |
| **熔断器** | 连续 3 次失败后停止 | 自动检索/压缩加计数器上限 |
| **一次性质量助推** | `<analysis>` 打草稿后丢弃 | chain-of-thought 生成后只保留结论 |
| **区分数据搬运 vs 信息精炼** | COMPACTABLE_TOOLS 列表 | 区分原始 chunk 和已总结的内容 |
| **缓存感知压缩** | cached MC 不修改本地消息 | 压缩时考虑对 KV cache / prompt cache 的影响 |
| **压缩后恢复关键上下文** | 自动重读 5 个文件 | 压缩后重新注入最关键的 N 个 chunk |

# 第五课：System Prompt 工程 — 缓存感知的咒语书

> 核心文件：`src/constants/prompts.ts`（1000+行）
> 缓存基础设施：`src/constants/systemPromptSections.ts`（68行）
> API 层分割：`src/utils/api.ts` `splitSysPromptPrefix()`

---

## 一、核心发现：Prompt 是数组，不是字符串

`getSystemPrompt()`（prompts.ts L444）返回的是 `string[]`，不是单个字符串。每个元素是一个独立的 section，按**缓存语义**分段：

```
返回值: string[]

[
  // ─── 石板篇（Static, cacheable, scope: 'global'）───
  getSimpleIntroSection()        → 角色定义 + 安全约束
  getSimpleSystemSection()       → 工具执行模型 + 权限模式 + 标签解释
  getSimpleDoingTasksSection()   → 代码风格规矩（ant 用户有额外指令）
  getActionsSection()            → 危险操作确认规则
  getUsingYourToolsSection()     → 专用工具 > Bash
  getSimpleToneAndStyleSection() → 别用 emoji、引用带行号
  getOutputEfficiencySection()   → 简洁直接（ant 用户完全不同的风格）

  // ─── 分割线 ───
  SYSTEM_PROMPT_DYNAMIC_BOUNDARY     ← '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'

  // ─── 活页篇（Dynamic, per-user/session, scope: null）───
  session_guidance     → 工具相关的条件性指令（AgentTool、SkillTool 等）
  memory               → 用户记忆文件
  ant_model_override   → 内部用户模型配置
  env_info_simple      → 环境信息（OS、shell、cwd、模型名）
  language             → 语言偏好
  output_style         → 输出风格
  mcp_instructions     → MCP 服务器指令（DANGEROUS_uncached!）
  scratchpad           → 临时文件目录
  frc                  → 函数结果清理
  ...
]
```

---

## 二、缓存分割机制

### DYNAMIC_BOUNDARY 的作用

`prompts.ts` L105-115：

```typescript
/**
 * Everything BEFORE this marker can use scope: 'global'.
 * Everything AFTER contains user/session-specific content.
 *
 * WARNING: Do not remove or reorder without updating:
 * - src/utils/api.ts (splitSysPromptPrefix)
 * - src/services/api/claude.ts (buildSystemPromptBlocks)
 */
export const SYSTEM_PROMPT_DYNAMIC_BOUNDARY =
  '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'
```

### API 层如何使用

`api.ts` L321 的 `splitSysPromptPrefix()` 拿到数组后，按三种模式处理：

**模式 1：有 MCP 工具（跳过全局缓存）**
```
attribution header → cacheScope: null
system prompt prefix → cacheScope: 'org'（组织级）
其余全部拼接 → cacheScope: 'org'
```

**模式 2：全局缓存模式（1P 用户，找到 boundary）**
```
attribution header → cacheScope: null
system prompt prefix → cacheScope: null
boundary 之前的内容 → cacheScope: 'global'  ← 全用户共享！
boundary 之后的内容 → cacheScope: null
```

**模式 3：默认模式（3P 或无 boundary）**
```
attribution header → cacheScope: null
system prompt prefix → cacheScope: 'org'
其余全部拼接 → cacheScope: 'org'
```

**`cacheScope: 'global'`** 意味着**全平台所有用户共享**同一份 KV Cache——不是同一个用户的多次调用，而是跨用户共享。这就是石板篇不能包含任何用户特定信息的原因。

---

## 三、Section 缓存基础设施

`systemPromptSections.ts`（仅 68 行，但意义重大）：

```typescript
// 普通 section：计算一次，缓存到 /clear 或 /compact
function systemPromptSection(name, compute) {
  return { name, compute, cacheBreak: false }
}

// 危险 section：每轮重新计算，会打碎缓存
function DANGEROUS_uncachedSystemPromptSection(name, compute, _reason) {
  return { name, compute, cacheBreak: true }
}
```

### 解析逻辑

```typescript
async function resolveSystemPromptSections(sections) {
  return Promise.all(sections.map(async s => {
    if (!s.cacheBreak && cache.has(s.name)) {
      return cache.get(s.name)  // 已计算过，直接取缓存
    }
    const value = await s.compute()
    cache.set(s.name, value)    // 首次计算，存入缓存
    return value
  }))
}
```

### `DANGEROUS_` 前缀的设计意义

整个代码库中只有 `mcp_instructions` 用了危险版：

```typescript
DANGEROUS_uncachedSystemPromptSection(
  'mcp_instructions',
  () => getMcpInstructionsSection(mcpClients),
  'MCP servers connect/disconnect between turns'  // ← 强制写明原因
)
```

`_reason` 参数是一个**强制文档机制**——开发者必须写明为什么这个 section 需要每轮重算。防止"随手加个条件"毁掉缓存效率。

---

## 四、2^N 缓存碎片化问题

`prompts.ts` L343-351 的注释：

> Session-variant guidance that would **fragment the cacheScope:'global' prefix** if placed before SYSTEM_PROMPT_DYNAMIC_BOUNDARY. Each conditional here is a runtime bit that would otherwise **multiply the Blake2b prefix hash variants (2^N)**.

如果有 5 个条件性指令放在 boundary 之前：

```
条件 A: 有/没有 AgentTool
条件 B: 有/没有 SkillTool
条件 C: 是/不是非交互模式
条件 D: fork subagent 是否启用
条件 E: 是否有 verification agent

缓存变体数 = 2^5 = 32 种不同的前缀哈希
缓存命中率从 ~100% 暴跌到 ~3%
```

**解决方案**：所有依赖运行时状态的指令**全部推到 boundary 之后**。

这就是 `getSessionSpecificGuidanceSection()`（L352-400）存在的原因——它把所有条件性内容聚合到 dynamic 段，不污染 static 段的缓存。

---

## 五、内部用户 vs 外部用户的 A/B 分流

`process.env.USER_TYPE === 'ant'` 出现 8+ 次，每次给内部用户额外的指令：

| 位置 | 外部用户 | 内部用户额外得到的 |
|------|---------|-------------------|
| L201-203 | 通用代码风格 | "默认不写注释。只在 WHY 不明显时加" |
| L207-209 | — | "不解释 WHAT（好的命名已经做到了）" |
| L211 | — | "报告完成前必须验证：跑测试、执行脚本" |
| L227 | — | "发现用户的请求基于误解时说出来" |
| L237-241 | — | "如果测试失败就说失败，不隐瞒不美化" |
| L394 | — | 独立对抗性验证 Agent |
| L404-414 | "Go straight to the point. Be extra concise." | 一整段关于"像给人写信一样写"的长指令 |
| L529-537 | — | 数字长度锚点（"工具调用间 ≤25 词"） |

**最有趣的对比**：

外部用户输出指令（L416-427）：
```
IMPORTANT: Go straight to the point. Be extra concise.
Keep your text output brief and direct.
```

内部用户输出指令（L404-414）：
```
When sending user-facing text, you're writing for a person, not logging
to a console. Assume users can't see most tool calls or thinking...
Write user-facing text in flowing prose while eschewing fragments,
excessive em dashes, symbols and notation...
What's most important is the reader understanding your output without
mental overhead or follow-ups, not how terse you are.
```

Anthropic 内部工程师觉得**太简洁反而不好用**——用户需要理解，而不只是速度。

---

## 六、其他精妙设计

### Undercover Mode 的 prompt 净化

`prompts.ts` L620-628, L660-667：

```typescript
if (process.env.USER_TYPE === 'ant' && isUndercover()) {
  // suppress — 不在 prompt 中暴露模型名和 ID
}
```

内部员工在公开仓库工作时，system prompt 中的模型名/ID 全部隐藏，防止泄露到公开 commit/PR。

### 环境信息的平台适配

`prompts.ts` L739-742：

```typescript
if (env.platform === 'win32') {
  return `Shell: ${shellName} (use Unix shell syntax, not Windows
    — e.g., /dev/null not NUL, forward slashes in paths)`
}
```

Windows 用户额外提醒用 Unix 语法。

### Agent 子 prompt 的精简

`prompts.ts` L758-770 的 `DEFAULT_AGENT_PROMPT`：

```
Complete the task fully—don't gold-plate, but don't leave it half-done.
Respond with a concise report covering what was done and key findings —
the caller will relay this to the user, so it only needs the essentials.
```

子 agent 的 prompt 比主 agent 简短得多——只有核心行为规范。配合第三课的"手脑分离"原则：子 agent 不需要完整的规则上下文。

### 模型知识截止日期

`prompts.ts` L712-730：

```typescript
function getKnowledgeCutoff(modelId) {
  if (canonical.includes('claude-opus-4-6'))   return 'May 2025'
  if (canonical.includes('claude-sonnet-4-6')) return 'August 2025'
  if (canonical.includes('claude-haiku-4'))    return 'February 2025'
  // ...
}
```

每个模型硬编码知识截止日期，注入到 prompt 中。注释标记 `@[MODEL LAUNCH]` 提醒每次发布新模型时更新。

---

## 七、可复用的设计原则

| 原则 | Claude Code 的体现 | 你的系统如何应用 |
|------|-------------------|----------------|
| **Prompt 是数组不是字符串** | 按缓存语义分段，不按逻辑分段 | 把 system prompt 设计为 section 数组 |
| **Static/Dynamic 分割** | boundary 标记分隔可缓存和不可缓存部分 | 所有用户通用的内容放前面，个性化内容放后面 |
| **2^N 碎片化防御** | 条件内容全部推到 boundary 之后 | 不在缓存段里放任何 if-else |
| **DANGEROUS_ 前缀** | 强制写明为什么需要每轮重算 | 打碎缓存的操作必须有书面理由 |
| **A/B 在 prompt 层** | USER_TYPE + GrowthBook 条件注入不同 section | 不改代码，改 prompt section 就能 A/B 测试 |
| **Section 缓存** | 普通 section 只计算一次 | 不变的 prompt 段不要每轮重新拼接 |
| **环境感知** | 平台、shell、模型、知识截止都注入 prompt | 让模型知道它在什么环境下工作 |

---

## 八、成本估算

假设 static 段 50K token：

| 场景 | prompt cache | 每次调用的 prompt 处理成本 |
|------|-------------|-------------------------|
| 全局缓存命中 | 50K token 免费读取 | 仅处理 dynamic 段（~5-10K） |
| 缓存碎片化（5 个条件在 static 段） | 50K/32 ≈ 1.5K 命中 | 处理 ~48.5K + dynamic |
| 无缓存 | 0 | 处理全部 ~55-60K |

全球数百万用户，每人每天数十次调用——缓存命中率每提升 1%，节省的计算量以 **Gtok/day** 计。这就是为什么 Claude Code 对 prompt 结构如此执着。

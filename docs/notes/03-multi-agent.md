# 第三课：多 Agent 协作 — 分身术

> 核心文件：`src/tools/AgentTool/forkSubagent.ts`（211行）、`runAgent.ts`（855行）、`AgentTool.tsx`
> 通信文件：`src/tools/SendMessageTool/`

---

## 一、三种分身方式

```
AgentTool 入口（AgentTool.tsx L322）
  │
  ├─ subagent_type 省略 + fork gate 开 → Fork（完美克隆）
  │    继承父亲的 system prompt、工具池、对话历史
  │    后台运行，权限冒泡到父亲终端
  │
  ├─ subagent_type = "Explore"/"Plan"/自定义 → Fresh（轻装上阵）
  │    独立 system prompt，空上下文或最近 N 条消息
  │    去掉不需要的 CLAUDE.md 和 gitStatus 省 token
  │
  └─ TeamCreate + SendMessage → Team（命名团队）
       多个命名 agent，通过 SendMessage 工具显式通信
       文本输出对其他 agent 不可见，必须用工具通信
```

---

## 二、Fork 路径的核心设计

### 全部为缓存一致性服务

Fork 的每一个设计决策都围绕一个目标：**让所有 fork 子 agent 的 API 请求前缀字节一致，命中同一个 prompt cache。**

| 决策 | 原因 |
|------|------|
| `getSystemPrompt: () => ''`，传父亲的渲染后字节 | 重新调用 `getSystemPrompt()` 可能因 GrowthBook 状态变化产生字节差异 |
| 保留 AgentTool 不移除，运行时用 `isInForkChild` 拦截 | 移除一个工具 → tool_defs 字节变 → 从 tool_defs 开始所有后续 token 的 KV 全部重算 |
| 保留 thinking block 不删除 | thinking block 有加密签名，篡改会 API 报错；且字节变化导致 cache miss |
| 所有 tool_result 用同一个占位符 `'Fork started — processing in background'` | 不同占位符 → 前缀分歧 → cache miss |
| `model: 'inherit'` 使用父亲同模型 | 不同模型可能导致不同的 API 端点和缓存空间 |

### buildForkedMessages 的构造

```
[...父亲的对话历史]（字节一致 ✓）
assistant 消息（完整克隆，含所有 tool_use + thinking）（字节一致 ✓）
user 消息:
  tool_result(id_1, "Fork started — processing in background")  （字节一致 ✓）
  tool_result(id_2, "Fork started — processing in background")  （字节一致 ✓）
  tool_result(id_3, "Fork started — processing in background")  （字节一致 ✓）
  text: "<fork_boilerplate>规则...</fork_boilerplate>\n指令: ..."  （← 唯一不同的部分）
```

N 个 fork 子 agent 中，只有最后的指令 text block 不同。前面 99%+ 的字节相同，命中缓存。

### 防递归分叉

```typescript
// forkSubagent.ts L78-89
export function isInForkChild(messages): boolean {
  // 检查消息历史中有没有 <fork_boilerplate> 标签
  return messages.some(m => content.includes(`<${FORK_BOILERPLATE_TAG}>`))
}
```

工具池保留 AgentTool（缓存一致性），但运行时检测 fork 标记拦截递归调用。

### 子 agent 行为规范（纯 prompt 控制）

```
STOP. READ THIS FIRST.
You are a forked worker process. You are NOT the main agent.
1. Your system prompt says "default to forking." IGNORE IT — that's for the parent.
6. Do NOT emit text between tool calls. Use tools silently, then report once at the end.
8. Keep your report under 500 words.
9. Your response MUST begin with "Scope:".
```

Rule 1 解决了一个微妙问题：子 agent 继承了父亲的 system prompt，里面有 "default to forking" 指令。必须显式告诉子 agent 忽略它——"那是给你爸的，不是给你的"。

---

## 三、Fresh 路径的成本优化

### Explore/Plan 去掉不需要的上下文

```typescript
// runAgent.ts L385-410
// 去掉 CLAUDE.md（commit/PR/lint 规则）
// 理由：只读搜索 agent 不需要执行规则，主 agent 负责解读
// 节省：~5-15 Gtok/week（3400万+ Explore 调用）
const shouldOmitClaudeMd = agentDefinition.omitClaudeMd

// 去掉 gitStatus（可达 40KB）
// 理由：如果需要，Explore 自己跑 `git status` 拿新鲜数据
// 节省：~1-3 Gtok/week
const resolvedSystemContext = (type === 'Explore' || type === 'Plan')
  ? systemContextNoGit : baseSystemContext
```

**设计哲学**：子 agent 是"手"（执行搜索/读取），主 agent 是"脑"（解读结果、做决策）。手不需要知道规则。

### 权限不泄漏

```typescript
// runAgent.ts L466-479
alwaysAllowRules: {
  cliArg: parent.alwaysAllowRules.cliArg,  // 保留 SDK 级别权限
  session: [...allowedTools],               // 替换（不继承父亲的 session 权限）
}
```

父亲在会话中临时允许的工具不会自动传给子 agent。

---

## 四、Team 通信协议

### 基本通信

```json
{"to": "researcher", "message": "start task #1"}   // 点对点
{"to": "*", "message": "all stop"}                   // 广播（慎用）
```

关键规则：agent 的普通文本输出**对其他 agent 不可见**。想通信必须显式调用 SendMessage。避免"意外通信"。

### 优雅关闭协议

```json
// 请求关闭
{"type": "shutdown_request", "request_id": "..."}
// 同意（agent 进程终止）
{"type": "shutdown_response", "approve": true}
// 拒绝（继续工作）
{"type": "shutdown_response", "approve": false}
```

---

## 五、KV Cache 原理（为什么字节一致性如此重要）

### Transformer 注意力计算

每个 token 被转换为 Q(Query)、K(Key)、V(Value) 三个向量。生成第 N 个 token 时，用 Qₙ 对所有前面 token 的 K 做注意力，加权求和 V。

前面 token 的 K 和 V **只取决于它们自己的内容和位置**，和后面的 token 无关。所以算过一次可以缓存 → **KV Cache**。

### 推理级 KV Cache（单次请求内）

处理 prompt 时计算所有 token 的 K, V 存入缓存。后续每生成一个 token 只需计算增量。

### API 级 Prompt Cache（跨请求）

Anthropic 将 KV Cache 扩展到**跨请求**：如果两个请求的前缀字节完全一致，第二个请求可以直接加载第一个请求已计算的 KV，只计算分歧点之后的部分。

```
请求 A: [AAAA BBBB CCCC DDDD xxxx]
请求 B: [AAAA BBBB CCCC DDDD yyyy]
                                ↑ 分歧点
         ├── 缓存命中（免费）───┤├─ 重算 ─┤
```

**匹配条件**：前缀**逐字节相同**。一个字节不同 → tokenization 可能不同 → 位置编码偏移 → 该位置之后所有 token 的 KV 全部失效。

### 缓存一致性的成本影响

假设 200K token 的上下文，5 个 fork 子 agent：

| 方案 | 每个子 agent 需计算 | 5 个合计 |
|------|-------------------|---------|
| 前缀一致（仅指令不同） | ~50 token | ~250 token |
| 前缀不一致（移除一个工具） | ~199K token | ~995K token |

差距 **4000 倍**。这就是 Claude Code 对缓存一致性执着的原因。

---

## 六、可复用的设计原则

| 原则 | Claude Code 的体现 |
|------|-------------------|
| **缓存一致性优先** | 所有 fork 设计都围绕"前缀字节一致"，代码清洁度让位 |
| **运行时拦截 > 编译时移除** | 保留 AgentTool + isInForkChild 检查，而非从工具池移除 |
| **传渲染结果不传生成函数** | 传 renderedSystemPrompt 字节，不重新调用 getSystemPrompt() |
| **手脑分离** | 子 agent 执行，主 agent 决策。子 agent 不需要规则上下文 |
| **权限不泄漏** | 父亲的 session 权限不自动传给子 agent |
| **显式通信** | 文本输出不可见，必须用 SendMessage 工具 |
| **prompt 控制行为** | 子 agent 的规范写在 prompt 里，不用代码分支 |

---

## 七、关于 LLM 的本质

> "LLM 始终就像一个猜词器"

从工程角度看确实如此。KV Cache 的本质就是：既然前面的词已经"猜"过了（计算过 K, V），下一个词猜的时候直接复用，不用重新处理前文。Prompt Cache 进一步说：如果两次猜词的前文一模一样，第二次可以直接跳到分歧点开始猜。

整个 Claude Code 的多 Agent 架构——fork、占位符、缓存一致性——本质上都是在优化"猜词器"的前文处理效率。模型能力再强，底层仍是 next token prediction。工程的价值在于：**在猜词器的约束下，用最少的计算量完成最多的工作。**

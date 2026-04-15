# 第一课：Agent Loop — 引擎的心跳

> 源码文件：`src/query.ts`（~70KB，1730 行）
> 辅助文件：`src/QueryEngine.ts`（SDK/headless 版本）

---

## 一、全景结构

`while(true)` 循环（L307-L1728），每一次迭代 = 一次"思考→行动→观察"。

```
① 准备（L308-549）     解构状态 + 四级预压缩（snip → micro → collapse → auto）
② 泵血（L650-863）     流式调用 Claude API，边接收边检测 tool_use，边启动工具执行
③ 分诊（L1062-1357）   没有工具调用？走 7 个 continue site 或正常 return
④ 干活（L1360-1520）   有工具调用？执行工具，收集结果
⑤ 回流（L1714-1728）   组装新 State，continue 回到 ①
```

---

## 二、三个核心设计模式

### 模式 1：状态拍照重建（Immutable State Reconstruction）

**位置**：L308-321

```typescript
let { toolUseContext } = state       // 唯一可变的
const {                               // 其余全部 const
  messages, turnCount, maxOutputTokensRecoveryCount,
  hasAttemptedReactiveCompact, maxOutputTokensOverride,
  stopHookActive, ...
} = state
```

**原则**：每轮循环开头从 `state` 对象解构，不用全局 mutable 变量。每个 continue site 构造一个完整的新 `State` 对象赋给 `state`，然后 `continue`。

**为什么**：7 个 continue site 中的任何一个都可能跳回循环顶部。如果用 mutable 变量，每个 continue site 都要手动重置/保留所有变量——遗漏一个就是 bug。用 State 对象，每个 continue site **显式声明完整的下一轮状态**，不可能遗漏。

**推论**：L1716 用 `[...messagesForQuery, ...assistantMessages, ...toolResults]` 构造新数组，而不是 `push` 到原数组，同理。如果直接修改 `messagesForQuery`，当 continue site 触发时它已经被污染——混进了不该出现在恢复路径中的数据。

**一句话**：不修改，只创建新的。

---

### 模式 2：Withheld Errors（扣住可恢复错误）

**位置**：L788-825

```typescript
let withheld = false
if (contextCollapse?.isWithheldPromptTooLong(message))  withheld = true
if (reactiveCompact?.isWithheldPromptTooLong(message))  withheld = true
if (isWithheldMaxOutputTokens(message))                 withheld = true

if (!withheld) {
  yield yieldMessage     // 只有不可恢复的错误才暴露给用户
}
assistantMessages.push(message)  // 无论如何都记录（恢复逻辑要检查）
```

**原则**：可恢复的错误不暴露给用户，先静默尝试修复。修复成功→用户无感知；修复失败（L1173）→才显示错误。

**关键细节**：被扣住的错误仍然 push 到 `assistantMessages`，因为后面的恢复逻辑（L1062-1070）需要检查 `lastMessage` 的类型来决定走哪条恢复路径。

**类比**：医生发现异常，先不通知家属，自己先治。

---

### 模式 3：流式工具执行（Streaming Tool Execution）

**位置**：L838-862

```typescript
// 模型正在流式输出，每收到一个 tool_use block
for (const toolBlock of msgToolUseBlocks) {
  streamingToolExecutor.addTool(toolBlock, message)  // 立刻开始执行
}

// 同时检查已完成的工具结果
for (const result of streamingToolExecutor.getCompletedResults()) {
  yield result.message
  toolResults.push(...)
}
```

**原则**：不等模型输出全部完成再执行工具。模型说"读 A 文件"的时候，A 的读取在模型还在生成"写 B 文件"的参数时就已经开始了。

**降级回退**（L1380-1382）：

```typescript
const toolUpdates = streamingToolExecutor
  ? streamingToolExecutor.getRemainingResults()   // 流式：收集剩余
  : runTools(toolUseBlocks, ...)                  // 批量：串行执行全部
```

流式执行是默认路径，批量是降级回退。

---

## 三、7 个 Continue Sites

| # | 行号 | 触发条件 | 恢复策略 | 防重入机制 |
|---|------|---------|---------|-----------|
| 1 | L950 | FallbackTriggeredError | 换备用模型重试 | `attemptWithFallback` 布尔门卫 |
| 2 | L1115 | prompt_too_long + 有 collapse 可排 | 排掉已暂存的 context collapse | `transition.reason !== 'collapse_drain_retry'` |
| 3 | L1165 | prompt_too_long + collapse 排完还不够 | 全量反应式压缩 | `hasAttemptedReactiveCompact = true` |
| 4 | L1220 | max_output_tokens + 首次触发 | 8K → 64K 升级 | `maxOutputTokensOverride === undefined`（一次性） |
| 5 | L1251 | max_output_tokens + 已升级 | 注入 meta 消息"接着写" | `maxOutputTokensRecoveryCount < LIMIT` |
| 6 | L1305 | stop hook 返回阻塞错误 | 注入 hook 错误消息重试 | `hasAttemptedReactiveCompact` 保留不重置 |
| 7 | L1340 | token budget 未用完 | 注入 nudge 消息继续 | `decision.action === 'continue'` |

**设计哲学**：先试便宜的方案（改参数），不行再试贵的方案（多轮对话/压缩），都不行才认输。

---

## 四、退出条件

| 行号 | reason | 含义 |
|------|--------|------|
| L646 | `blocking_limit` | token 达到阻塞上限，无法压缩 |
| L977 | `image_error` | 图片处理错误 |
| L996 | `model_error` | API 不可恢复错误 |
| L1051 | `aborted_streaming` | 用户中断（Ctrl+C） |
| L1175 | `prompt_too_long` | 压缩后仍超长 |
| L1264 | `completed` | API 错误但跳过 stop hook |
| L1279 | `stop_hook_prevented` | hook 阻止继续 |
| L1357 | `completed` | **正常完成**（最常见出口） |
| L1515 | `aborted_tools` | 工具执行中被中断 |
| L1520 | `hook_stopped` | hook 阻止继续 |
| L1711 | `max_turns` | 达到最大轮数 |

---

## 五、关键工程细节

### tool_use 检测不依赖 stop_reason

**位置**：L554-556

```typescript
// Note: stop_reason === 'tool_use' is unreliable -- it's not always set correctly.
```

实际检测方式（L829-834）：检查流式响应中有没有 `type === 'tool_use'` 的 content block。

**原则**：不信 metadata，信 data 本身。

### 防死循环的工程教训

**位置**：L1291-1297 注释

```
// Resetting to false here caused an infinite loop:
// compact → still too long → error → stop hook blocking → compact → …
// burning thousands of API calls.
```

修复：stop hook 的 continue site 中**保留** `hasAttemptedReactiveCompact` 标记，不重置。

**原则**：每个恢复策略必须有防重入机制（布尔门卫、计数器、或状态检查），否则 continue site 之间会形成无限循环。

### 流式降级时的 tombstone 机制

**位置**：L712-740

模型降级（fallback）发生在流式过程中时，之前输出的 assistant messages 已经有了无效的 thinking block 签名。解决方式：yield `tombstone` 消息通知 UI 删除这些残留消息，然后清空所有累积状态重新开始。

---

## 六、可复用的设计原则总结

| 原则 | 在 Claude Code 中的体现 | 应用场景 |
|------|------------------------|---------|
| **Immutable State** | State 对象拍照重建，新数组不 push | 任何有多分支跳转的状态机 |
| **Withheld Errors** | 可恢复错误静默处理，不惊扰用户 | Agent UX 设计 |
| **Streaming Execution** | 边接收模型输出边执行工具 | 降低端到端延迟 |
| **分级恢复** | 便宜方案 → 贵方案 → 放弃 | 容错设计 |
| **防重入门卫** | 布尔/计数器防止 continue site 成环 | 任何有重试逻辑的循环 |
| **不信 metadata 信 data** | 用 content block 判断，不用 stop_reason | 外部 API 集成 |
| **Tombstone 清理** | 降级时通知 UI 删除无效消息 | 流式 UI 的状态一致性 |

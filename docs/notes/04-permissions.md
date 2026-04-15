# 第四课：权限系统 — 看门人的七道关卡

> 核心文件：`src/services/tools/toolExecution.ts`（1300+行）
> Bash 安全：`src/tools/BashTool/bashSecurity.ts`（2300+行）、`bashPermissions.ts`（1800+行）
> 工具接口：`src/Tool.ts`（793行）

---

## 一、七步权限流水线

`toolExecution.ts` 的 `checkPermissionsAndCallTool`（L599）：

```
模型输出 tool_use block
  │
Step 1 (L614-680): Zod 输入验证
  │  schema.safeParse(input) — 格式不对直接拒绝
  │
Step 2 (L682-733): 工具自带验证
  │  tool.validateInput() — 每个工具的自定义逻辑
  │
Step 3 (L740-752): 投机性分类器启动（仅 Bash）
  │  提前启动 AI 分类器，和后续步骤并行运行
  │
Step 4 (L756-773): 防注入清理
  │  剥离模型可能注入的内部字段（defense-in-depth）
  │
Step 5 (L800-862): PreToolUse Hooks
  │  用户配置的钩子，可放行/拒绝/修改输入/停止
  │
Step 6 (L920-1104): 权限决策
  │  综合 hook 结论、规则匹配、分类器、用户弹窗
  │
Step 7 (L1128+): 执行工具
  │  使用可能被 hook 或用户修改过的输入
```

---

## 二、Fail-Closed 默认值

`Tool.ts` L757-769：

```typescript
const TOOL_DEFAULTS = {
  isConcurrencySafe: () => false,  // 不确定？假设不安全
  isReadOnly: () => false,         // 不确定？假设有写操作
  isDestructive: () => false,
  checkPermissions: () => ({ behavior: 'allow', updatedInput: input }),
}
```

`isConcurrencySafe` 和 `isReadOnly` 默认 false：新工具的作者忘了声明 → 不允许并发、不当作只读。**不确定的一律按最危险的情况处理。**

`checkPermissions` 默认 allow 不矛盾：这是工具**自己的**额外检查，默认 allow = "我没有额外限制，交给通用权限系统（Step 6）判断"。

---

## 三、Bash 的纵深防御

### 23 种安全检查

`bashSecurity.ts` 定义了 23 种检查 ID，覆盖：

| 类别 | 检查 |
|------|------|
| 命令结构 | 不完整命令、混淆标志、Shell 元字符 |
| 注入向量 | 命令替换、IFS 注入、环境变量、brace 展开 |
| 重定向 | 输入/输出重定向（防覆盖 .bashrc 等） |
| 编码攻击 | Unicode 空白、控制字符、反斜杠转义 |
| 特定工具 | jq system()、git commit 替换、/proc/environ |
| zsh 专用 | zmodload、sysopen、ztcp 等 14 个危险命令 |
| 解析差异 | 注释/引号不同步、引号内换行 |

### tree-sitter AST 解析

`bashPermissions.ts` L1670-1780，用真正的语法解析器分析命令：

```
simple        → 干净的简单命令，可信任
too-complex   → 有命令替换/扩展/控制流，无法静态分析 → 问用户
parse-unavailable → tree-sitter 不可用，回退到正则
```

**关键**：即使 AST 判定 too-complex，deny 规则仍然执行（L1746）：

```typescript
const earlyExit = checkEarlyExitDeny(input, toolPermissionContext)
if (earlyExit !== null) return earlyExit  // deny 不因"无法解析"而降级
return { behavior: 'ask', ... }            // 无法分析且没命中 deny → 问用户
```

deny（直接拒绝）比 ask（弹窗询问）更严格。如果因为"无法解析"就把 deny 降级为 ask，等于削弱了用户明确配置的安全策略。攻击者可以构造 AST 上"太复杂"的命令来绕过 deny 规则。

### 内部字段防注入

`toolExecution.ts` L756-773：

```typescript
// _simulatedSedEdit 只应由权限系统注入，Zod strictObject 理论上已拦截
// 但这里再手动剥离一次——防止未来改了 schema 忘了 strict 约束
if ('_simulatedSedEdit' in processedInput) {
  const { _simulatedSedEdit: _, ...rest } = processedInput
  processedInput = rest
}
```

**不信任理论保证**，在运行时再加一道防线。注释明确说：`safeguard against future regressions`。

---

## 四、投机性分类器（Speculative Classifier）

### 工作原理

```
Step 3: startSpeculativeClassifierCheck(command)
  │  启动 side_query（小模型），和 Step 4-5 并行运行
  │
  ├─ Step 4-5 跑完...
  │
Step 6: consumeSpeculativeClassifierCheck(command)
  │  分类器大概率已经跑完，直接取结果
  │  如果没跑过（Step 3 条件不满足），现在跑
```

### 放行标准

```typescript
// bashPermissions.ts L1575-1586
if (classifierResult.matches && classifierResult.confidence === 'high') {
  return { type: 'classifier', classifier: 'bash_allow' }
}
```

只有 **high confidence** 才自动放行。medium/low → 弹窗问用户。

原因：自动放行 = 用户没有拦截机会。分类器本身是小模型，也会犯错。high confidence 误放行的概率可接受，medium 不行。

**原则：自动化的边界画在"错了也没事"的地方。**

### 投机失败的成本

如果 Step 5 的 hook 已经给出 allow 决策，分类器结果被**直接丢弃**。浪费了一次 side_query 调用，但这个成本远低于"没有投机、每次都等分类器"的延迟成本。

**期望值为正就值得做。** 大多数情况下 hook 不给出决策，分类器结果会被用到。

---

## 五、权限决策的优先级

```
1. Hook 已决策（allow/deny）     → 直接用，跳过后续
2. 精确匹配 deny 规则            → deny（最高优先级）
3. 前缀/通配符 deny 规则         → deny
4. 精确匹配 allow 规则           → allow
5. 前缀/通配符 allow 规则        → allow
6. 分类器 high confidence allow  → allow
7. 以上都没匹配                  → ask（弹窗问用户）
```

**deny 永远优先于 allow。** 即使在 bypass 模式下，安全敏感路径（.git/、.bashrc）仍强制检查。

---

## 六、权限决策结果类型

```typescript
type PermissionDecisionReason =
  | { type: 'hook'; hookName: string }       // Hook 决定的
  | { type: 'classifier'; reason: string }   // AI 分类器决定的
  | { type: 'auto_mode' }                    // auto 模式自动决定
  | { type: 'user_input' }                   // 用户弹窗决定的
  | { type: 'other'; reason: string }        // 其他（AST too-complex 等）
  | { type: 'default' }                      // 默认决策
```

每个决策都带有原因——用于遥测和审计。

---

## 七、PermissionDenied Hook 的重试机制

`toolExecution.ts` L1073-1101：

```typescript
// auto 模式分类器拒绝后，运行 PermissionDenied hooks
// 如果 hook 返回 { retry: true }，告诉模型可以重试
if (classifierResult.decisionReason?.classifier === 'auto-mode') {
  for await (const result of executePermissionDeniedHooks(...)) {
    if (result.retry) hookSaysRetry = true
  }
  if (hookSaysRetry) {
    // 注入 meta 消息："hook 说这个命令现在可以了，你可以重试"
  }
}
```

场景：auto 模式的分类器拒绝了一个命令，但 PermissionDenied hook（比如用户配置的"如果是在测试目录就放行"的逻辑）判断其实可以。hook 可以"翻案"，让模型重试。

---

## 八、可复用的设计原则

| 原则 | Claude Code 的体现 | 通用应用 |
|------|-------------------|---------|
| **Fail-closed 默认** | isConcurrencySafe/isReadOnly 默认 false | 新组件忘了配置时应走最安全路径 |
| **纵深防御** | Zod schema + 手动剥离内部字段 | 不信任单一防线，每层都检查 |
| **deny 永远优先** | 即使无法解析命令也执行 deny 规则 | 安全策略不能被降级 |
| **投机执行** | 提前启动分类器和后续步骤并行 | 期望值为正的预计算都值得做 |
| **高确定性才自动化** | 只有 high confidence 才自动放行 | 自动化边界画在"错了也没事"的地方 |
| **决策可审计** | 每个 allow/deny 都带 reason | 出问题时能追溯决策链 |
| **Hook 可翻案** | PermissionDenied hook 支持 retry | 给用户配置的逻辑最终裁决权 |
| **AST > 正则** | tree-sitter 解析替代正则匹配 | 安全检查用真正的解析器，不用模式匹配 |

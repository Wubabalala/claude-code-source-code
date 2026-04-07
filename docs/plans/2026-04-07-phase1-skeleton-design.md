# Phase 1 骨架设计：最小可行的代码库助手 Agent

> **状态**：已通过 brainstorming 确认，待 writing-plans 转换为实施计划
> **日期**：2026-04-07
> **场景定位**：代码库助手（不是通用 RAG 框架）——采用 Grep + Read 工具化检索，不用向量 RAG
> **技术栈**：Python + Anthropic Claude API
> **代码位置**：`D:\tools\claude-code-source\agent\`

---

## Context

本设计是 6 课 Claude Code 源码学习后的第一个动手实践，对应 `docs/notes/00-summary-build-your-own-agent.md` 中的 Phase 1。目标是构建一个可运行的最小 agent，验证从源码学到的核心模式。

**关键决策**：
- 场景：**代码库助手**（非 RAG 知识库），与 Claude Code 的设计哲学完全对齐
- 语言：**Python**（用户技术栈）
- LLM：**Anthropic Claude**（与源码学习项目同源）
- 交互形态：**CLI REPL**

---

## 全局架构

### 一句话定义

Phase 1 构建一个能通过工具与外部世界交互的对话式 AI agent，区别于普通 chatbot 的关键是**能在一次用户输入内多轮调用工具直到得到答案**。

### 四个文件 = 四个器官

```
agent/
├── main.py      🫁 肺：REPL 循环、会话历史管理、异常兜底
├── loop.py      ❤️ 心脏：while(true) 状态机、工具检测、退出判断
├── tools.py     🤚 手：Tool 接口 + 3 个内置工具
├── prompt.py    🧠 大脑皮层：Static/Dynamic 分段的 system prompt
├── api.py       支持文件：Anthropic SDK 调用包装
├── types.py     支持文件：共享类型（State, Message, AgentResult）
├── requirements.txt
└── README.md
```

### 数据流（一次完整交互）

```
用户输入 → main.py 累积到 history → prompt.py 组装 system prompt
→ loop.py while循环 → API 调用 → 检测 content blocks 中的 tool_use
→ 有 tool_use? → tools.py 执行 → 结果塞回 messages → 回到 while 顶部
→ 没 tool_use? → 退出 loop → main.py 提取最终文本 → 显示给用户
```

**关键纪律**：每轮循环开始时，State 完整重建，不在旧 state 上 mutate。

### 与 Claude Code 源码的映射

| Phase 1 文件 | Claude Code 对应 | 简化了什么 |
|-------------|-----------------|-----------|
| `main.py` | `src/main.tsx` (808KB) | 去掉 React/Ink UI、OAuth、插件、配置管理 |
| `loop.py` | `src/query.ts` (70KB) | 7 continue site → 2 个；12 出口 → 3 个；流式 → 批量 |
| `tools.py` | `src/tools/` (40+ 工具) | 40+ → 3 个；7 步权限流水线 → 1 步 |
| `prompt.py` | `src/constants/prompts.ts` | 去掉 cache-break 追踪、A/B 分流、hooks section |

---

## loop.py 详细设计

### 解决的问题

实现"装了 2 个旁路瓣膜的心脏"：正常路径处理工具调用循环，异常路径有 2 个恢复点。

### State 模型（immutable）

```python
@dataclass(frozen=True)
class State:
    messages: tuple[Message, ...]   # tuple 强制不可变
    turn: int
    fallback_model_used: bool       # 瓣膜 1 用：是否已降级
    output_retries: int             # 瓣膜 2 用：输出 token 已重试次数
    transition_reason: str          # 调试用："为什么进入这一轮"
```

用 `tuple` 而非 `list`、`frozen=True` 而非普通 dataclass，是为了**让违反 immutability 的写法编译期就报错**。

### 2 个 Continue Site（瓣膜）

| # | 名称 | 触发条件 | 行为 |
|---|------|---------|------|
| 1 | 模型降级回退 | 主模型 API 错误且 `is_recoverable(e) == True` | 切到 fallback 模型，**不告诉用户**（withheld error），continue 到 while 顶部 |
| 2 | 输出 token 恢复 | `stop_reason == "max_tokens"` 且无 tool_use 且 `output_retries < 3` | 让模型继续生成，重试计数 +1，continue |

### 3 个退出条件

| # | reason | 触发条件 |
|---|--------|---------|
| 1 | `completed` | 模型回答里没有 tool_use block（且不在 max_tokens 恢复路径） |
| 2 | `max_turns` | `state.turn > max_turns`（默认 25） |
| 3 | `model_error` | API 错误且降级也失败 / 不可恢复的错误 |

### Tool use 检测的关键纪律

**永远不依赖 `response.stop_reason`**。Claude Code 源码 L554 注释：`stop_reason === 'tool_use' is unreliable`。

正确做法：扫描 `response.content`，找 type 为 `tool_use` 的 block。

### Withheld errors 模式

可恢复错误（503、超时等）静默重试，不暴露给用户。不可恢复错误（401、400 等）必须暴露。`is_recoverable(e)` 函数集中判断。

### 工具调用流程（含权限）

```python
for tool_use in tool_use_blocks:
    tool = find_tool_by_name(tools, tool_use.name)

    # Step 1: 输入验证（Pydantic）
    try:
        validated = tool.input_model(**tool_use.input)
    except ValidationError as e:
        → 错误塞回 tool_result
        continue

    # Step 2: 权限检查
    if tool.check_permissions(validated) == DENY:
        → "Permission denied" 塞回 tool_result
        continue

    # Step 3: 真正执行
    try:
        result = tool.execute(validated)
        → result 塞回 tool_result
    except Exception as e:
        → 错误塞回 tool_result
```

**纪律**：工具错误**塞回 tool_result**，不抛给 loop.py。让模型成为最高级的错误处理器。

---

## tools.py 详细设计

### Tool 接口的 6 维度

每个工具回答 6 个问题：
1. **身份**：`name`
2. **能力**：`description()`
3. **参数 schema**：`input_model`（Pydantic）
4. **权限**：`check_permissions(input)`
5. **副作用属性**：`is_read_only(input)` / `is_concurrency_safe(input)` / `is_destructive(input)`
6. **执行**：`execute(input)`

**关键设计**：副作用属性是**方法**不是字段，因为可能依赖具体输入（例：bash 执行 `ls` 是只读，执行 `rm` 不是）。

**Fail-closed 默认值**：所有副作用属性默认 False（不确定就当作不安全）。

### 3 个工具

| 工具 | 职责 | is_read_only | is_concurrency_safe | 安全检查 |
|------|------|-------------|--------------------|---------|
| `read_file` | 读单个文件，支持 offset/limit | True | True | 拒绝敏感文件（.env, .git/, .ssh/, credentials） |
| `grep` | 用 ripgrep 搜文件 | True | True | 无（只读） |
| `bash` | 执行 shell 命令 | 启发式（依赖命令） | False | 危险命令黑名单（rm -rf /, fork bomb, mkfs, sudo, curl\|sh 等） |

### ToolResult 结构

```python
class ToolResult(BaseModel):
    output: str          # 给模型看的文本
    is_error: bool       # 是否错误
    metadata: dict       # 给观察者用，模型看不到
```

### 与 Claude Code 的对照

- ReadFileTool ← `src/tools/FileReadTool.ts`
- GrepTool ← `src/tools/GrepTool.ts`
- BashTool ← `src/tools/BashTool.ts`（最复杂的工具，源码有 23 种安全检查；Phase 1 只实现最小黑名单）

---

## prompt.py 详细设计

### 解决的问题

为 prompt cache 优化 system prompt 结构：static 段字节完全一致以命中缓存，dynamic 段隔离在缓存断点之后。

### Static / Dynamic 分段

```python
DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"

# Static 段：模块级常量字符串，绝对不变
STATIC_INTRO = "You are a code repository assistant..."
STATIC_TOOL_USAGE = "# Tools\n\n- read_file: ..."
STATIC_BEHAVIOR = "# Behavior\n\n- Be concise..."

# Dynamic 段：函数构建，每次可能不同
def build_dynamic_environment(cwd, os_name): ...
def build_dynamic_date(today): ...
```

### 主入口返回结构

```python
def build_system_prompt(cwd, os_name, today) -> list[dict]:
    return [
        {
            "type": "text",
            "text": <static 段拼接>,
            "cache_control": {"type": "ephemeral"},  # ← 显式缓存断点
        },
        {
            "type": "text",
            "text": <dynamic 段拼接>,
            # 没有 cache_control = 这块及之后不缓存
        },
    ]
```

### 5 条铁律

1. **Static 段禁止 if-else**：N 个条件 → 2^N 缓存碎片
2. **Static 段禁止 f-string 插入运行时变量**：插一个变量整段缓存失效
3. **Tools 字段也参与缓存**：工具描述必须静态、工具列表必须按字母序固定
4. **`cache_control` 是断点不是块**：标记的是"从开头到此处"的内容
5. **每次 turn 仍要调用 `build_system_prompt`**：static 段返回字节相同的字符串，cache 仍命中

### 缓存命中验证

每次 API 响应打印 `usage.cache_read_input_tokens` 和 `cache_creation_input_tokens`，让用户**眼睛能看到 cache 在工作**。

### 与 Claude Code 的对照

- `STATIC_INTRO` ← `getSimpleIntroSection()` (L175-184)
- `STATIC_TOOL_USAGE` ← `getSimpleSystemSection()` (L186-196)
- `STATIC_BEHAVIOR` ← `getSimpleDoingTasksSection()` (L199-243)
- `DYNAMIC_BOUNDARY` ← `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` (L114-115)
- 不实现：`DANGEROUS_uncached` 前缀、A/B 分流、hooks section（Phase 5+ 再加）

---

## main.py 详细设计

### 解决的问题

CLI 入口和会话级状态管理。**严格区分会话级 history 与 turn 级 state**：
- main.py 管理 **session**：跨 turn 的 `conversation_history`
- loop.py 管理 **turn**：单次循环内的 `State`

### 关键流程

```
init_client → get_tools (固定顺序) → REPL while true:
    1. 读用户输入
    2. 处理 slash 命令 (/exit, /reset)
    3. 把用户输入加入 history
    4. 构建 system prompt（每次 turn 都调用，但 static 段字节不变）
    5. 调用 run_agent_loop
    6. 根据 status 处理结果：
       - completed: 更新 history，显示最终文本
       - max_turns: 更新 history，提示达到上限
       - model_error: 不更新 history，提示错误
```

### 3 个容易出错的细节

1. **`model_error` 时不更新 history**：避免错误消息污染后续对话
2. **Ctrl+C 两层捕获**：input() 处退出 REPL，loop 内中止当前 turn
3. **system prompt 每次重建但 static 段字节不变**：模块级常量保证缓存命中

### 配置（Phase 1 硬编码）

```python
PRIMARY_MODEL = "claude-opus-4-6"
FALLBACK_MODEL = "claude-sonnet-4-6"
MAX_TURNS_PER_QUERY = 25
```

API key 从环境变量 `ANTHROPIC_API_KEY` 读取。

---

## 端到端验收清单

### 类别 1：基础功能（4 项）

| # | 输入 | 期望 |
|---|------|------|
| 1 | 读取 README.md 并告诉我项目作用 | 模型调用 read_file → 总结 |
| 2 | 搜索代码里所有 'TODO' | 模型调用 grep → 列出匹配 |
| 3 | 当前目录有多少个 python 文件 | 模型调用 bash 或 grep |
| 4 | 读 main.py，再搜索它定义的所有函数 | 多轮工具调用串联 |

### 类别 2:错误处理（4 项）

| # | 输入 | 期望 |
|---|------|------|
| 5 | 读取不存在的文件 | 错误塞回 tool_result，模型告知用户，不崩溃 |
| 6 | 执行 rm -rf / | 权限拒绝，不执行 |
| 7 | 读取 .env | 权限拒绝（敏感文件） |
| 8 | 断网状态提问 | 顶层 except 兜底，REPL 不退出 |

### 类别 3：架构纪律（5 项）

| # | 检查 | 验证方法 |
|---|------|---------|
| 9 | tool_use 检测不依赖 stop_reason | grep stop_reason loop.py 应只在 max_tokens 恢复处出现 |
| 10 | State 是 frozen | grep "frozen=True" types.py |
| 11 | 工具错误塞回 tool_result | 代码审查 tools.py 的 execute |
| 12 | tools 列表顺序固定 | 代码审查 get_tools() 返回 sorted(...) |
| 13 | static prompt 没有 f-string / 条件 | 代码审查 prompt.py 的 STATIC_* 常量 |

### 类别 4：缓存验证（4 项）

| # | 操作 | 期望 |
|---|------|------|
| 14 | 第 1 次问问题 | cache_create > 0, cache_read = 0 |
| 15 | 5 分钟内第 2 次问问题 | cache_read > 0 |
| 16 | 改 STATIC_INTRO 一行后重启 | 缓存全失效 |
| 17 | 等 6 分钟后再问 | TTL 过期，cache_create > 0 |

### 5 条铁律自检

| # | 铁律 | Phase 1 落地点 |
|---|------|---------------|
| 1 | content block 检测 tool_use | loop.py 的 `[b for b in response.content if b.type == "tool_use"]` |
| 2 | 工具结果用占位符替换不删除 | Phase 1 不删除任何消息（Phase 2 才用到） |
| 3 | Static prompt 禁止条件逻辑 | prompt.py 的 STATIC_* 是模块级常量 |
| 4 | Deny 永不降级为 Ask | BashTool.check_permissions 不解析失败时仍 deny |
| 5 | 压缩不是消毒 | Phase 1 没有压缩（Phase 2 才加） |

---

## Phase 1 不做的事（防 scope creep）

| 功能 | 推迟到 |
|------|-------|
| 流式 API 调用 | Phase 5 |
| 上下文压缩 | Phase 2 |
| 工具结果占位符替换 | Phase 2 |
| 完整 7 步权限流水线 | Phase 3 |
| 子 agent / Fork | Phase 4 |
| Session 持久化 | Phase 2 |
| MCP 集成 | 后续 |
| 配置文件 | Phase 2 |
| 多模型抽象 | 后续 |

**纪律**：每条都很有用，但任何一条加进 Phase 1 都会让 Phase 1 写不完。Phase 1 目标是"能跑"，不是"能用"。

---

## 完成定义

- [ ] 能完成"读 README → 解释项目"的端到端流程
- [ ] 5 条铁律全部落地
- [ ] 验收清单 17 项全部通过
- [ ] 代码总量约 500 行（4 核心 + 2 支持文件）

---

## 依赖

```
anthropic>=0.40.0
pydantic>=2.0.0
```

> 版本号据训练知识，安装前用 `pip index versions` 核实最新稳定版。

外部依赖：
- `ripgrep` (rg)：GrepTool 必需，未安装时工具会报错并提示安装链接

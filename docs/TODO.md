# TODO — Agent 项目改进清单

> 基于 Phase 1-7 完成后的严格自评（6.5/10），按优先级排列。
> 完成 P0 两项即可从 6.5 → 8 分。

---

## P0 致命缺陷（不修 = 面试被打）

### 1. 流式输出（Streaming）
**现状**：`run_agent_loop` 用 `client.messages.create()` 批量返回，用户等全部生成完才看到结果。长回复等 30s+ 没有任何反馈。
**目标**：改用 `client.messages.stream()` 或 `create(..., stream=True)`，逐 token 打印。
**改动面**：
- `agent/loop.py`：API 调用从 batch 改 stream；`for event in response` 逐块处理
- `agent/main.py`：REPL 输出从 `print(full_text)` 改为逐块 `print(chunk, end="", flush=True)`
- `agent/web.py`：Gradio ChatInterface 改用 generator `yield` 模式
- tool_use 检测：从 response.content 一次性读改为流中累积检测
- 估计 ~200 行改动 + ~15 测试

### 2. 代码编辑能力（WriteFile / EditFile 工具）
**现状**：只有 read_file / grep / bash 三个工具。叫"Code Repo Assistant"但不能写代码。
**目标**：加 `WriteFileTool`（整文件写入）和 `EditFileTool`（diff/patch 式编辑）。
**改动面**：
- `agent/tools.py`：新增两个 Tool 子类
  - WriteFileTool：writes_to_filesystem=True, destroys_data=False, check_permissions 过 hard_deny + ASK
  - EditFileTool：接受 old_string/new_string 做精确替换
- `agent/main.py`：注册到 get_tools()
- 安全：Phase 3 hard_deny 自动覆盖（is_hard_denied 检查写路径）
- 估计 ~150 行 + ~20 测试

---

## P1 重要改进（面试加分项）

### 3. 协议适配器模式
**现状**：`if not model.startswith("claude")` 硬编码展平 system/messages。
**改法**：抽象 `LLMAdapter` 接口，`AnthropicAdapter` / `OpenAICompatAdapter` 各自实现消息格式转换。loop.py 只和 adapter 交互。

### 4. main.py 重构
**现状**：`repl()` 函数 ~500 行，slash 命令用 if-elif 链。
**改法**：命令注册表 `{"/exit": handle_exit, "/agent": handle_agent, ...}`，每个命令独立函数，repl() 只做 dispatch。

### 5. 真实对抗测试
**现状**：290 测试全是 mock，没有 adversarial 输入。
**改法**：加 fuzz 测试（BashTool 路径提取边界）、prompt injection 测试（恶意 CLAUDE.md）、Unicode/encoding 绕过测试。

### 6. MCP 健壮化
**现状**：thread-based readline timeout 是 hack；无重连；无 resource API。
**改法**：改用 asyncio subprocess 通信；加连接断线自动重连（限 3 次）；补 resources/list 支持。

---

## P2 锦上添花

### 7. Session 过期清理
- `[session] rotation_keep_days = 30` 配置已有，但清理逻辑没实现
- 加 startup 时扫描 + 删除过期 JSONL

### 8. 复杂度路由（Phase 8 原计划）
- `agent/classifier.py`：规则 + 可选 LLM 分类器
- `agent/router.py`：complexity → model_name 映射
- LiteLLM config 里的 cheap/standard/powerful tier 已预留

### 9. Web UI 增强
- Gradio 加 tool call 可视化（展示 agent 调了哪些工具）
- 加权限审批对话框（替代 non_tty_default=ALLOW 的粗暴覆盖）
- 加 session 管理面板（/resume / /sessions 的 GUI 版）

### 10. Async 化
- `run_agent_loop` 改 async
- tool execution 可选并行（`asyncio.gather` for concurrent-safe tools）
- MCP client 改 asyncio subprocess

### 11. 多语言 system prompt
- 当前 prompt 全英文
- 检测用户语言偏好，切换 prompt 语言（或双语）

### 12. 自动记忆提取
- 当前 `/memory-save` 是手动的
- CC 风格：用 Haiku 自动从长对话中提取关键事实存入 memory.md

---

## 完成追踪

| # | 项目 | 优先级 | 状态 | 备注 |
|---|------|--------|------|------|
| 1 | Streaming | P0 | ❌ | 最高优先 |
| 2 | WriteFile / EditFile | P0 | ❌ | 第二优先 |
| 3 | 协议适配器 | P1 | ❌ | |
| 4 | main.py 重构 | P1 | ❌ | |
| 5 | 对抗测试 | P1 | ❌ | |
| 6 | MCP 健壮化 | P1 | ❌ | |
| 7 | Session 清理 | P2 | ❌ | |
| 8 | 复杂度路由 | P2 | ❌ | |
| 9 | Web UI 增强 | P2 | ❌ | |
| 10 | Async 化 | P2 | ❌ | |
| 11 | 多语言 prompt | P2 | ❌ | |
| 12 | 自动记忆提取 | P2 | ❌ | |

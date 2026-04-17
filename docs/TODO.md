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

## P1.5 重构遗留（P1 #3+#4 产生的技术债）

### 13. 集成级 adapter fallback 测试
**严重度**：中
**现状**：test_adapter.py 用 fake client 验证了单 adapter 的格式化+调用，但没有 "primary=claude fallback=deepseek，第一次成功第二次 fallback" 的端到端测试证明 adapter 在 loop 里真实切换。
**改法**：test_loop.py 加 1 case：primary 返回 recoverable error → fallback 用不同 model name → 断言 fake client 收到的 system 格式从 list[dict] 变为 str。

### 14. `to_anthropic_schema()` 重命名
**严重度**：低
**现状**：tools.py 上的方法还叫 `to_anthropic_schema`，语义上应该叫 `to_tool_schema`。adapter.build_tool_schemas 内部调它，名字不匹配。
**改法**：tools.py + mcp.py 重命名方法 + 更新 test_tools.py / test_mcp.py。~10 处改动。

### 15. usage 规范化预留
**严重度**：低
**现状**：ParsedResponse.usage 透传原始 SDK 对象。两个 adapter 同 SDK 所以短期无问题。如果加真 OpenAI SDK adapter，usage shape 泄漏是第一个 break 点。
**改法**：定义 `UsageInfo(input_tokens, output_tokens, cache_read, cache_create)` dataclass，adapter.call_model 返回时填充。print_cache_stats 读规范字段。

---

## 完成追踪

| # | 项目 | 优先级 | 状态 | 备注 |
|---|------|--------|------|------|
| 1 | Streaming | P0 | ✅ | `5138d85` + `1130c3f`（含 retry/fallback/error 提交边界标记） |
| 2 | WriteFile / EditFile | P0 | ✅ | `4a2abe6`（5 内建工具，能读能写能编辑） |
| 3 | 协议适配器 | P1 | ✅ | LLMAdapter ABC + AnthropicAdapter + FlattenedMessageAdapter + registry; loop/compact 每轮 get_adapter(model) |
| 4 | main.py 重构 | P1 | ✅ | AgentApp + 12 command handlers (commands.py) + client.py 提取; main.py 619→184 行 |
| 5 | 对抗测试 | P1 | ❌ | |
| 6 | MCP 健壮化 | P1 | ❌ | |
| 7 | Session 清理 | P2 | ❌ | |
| 8 | 复杂度路由 | P2 | ❌ | |
| 9 | Web UI 增强 | P2 | ❌ | |
| 10 | Async 化 | P2 | ❌ | |
| 11 | 多语言 prompt | P2 | ❌ | |
| 12 | 自动记忆提取 | P2 | ❌ | |
| 13 | adapter fallback 集成测试 | P1.5 | ❌ | loop 内 adapter 切换的端到端验证 |
| 14 | to_anthropic_schema 重命名 | P1.5 | ❌ | → to_tool_schema，~10 处 |
| 15 | usage 规范化 | P1.5 | ❌ | UsageInfo dataclass，预留给真 OpenAI adapter |

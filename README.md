# x402 LangGraph 支付 Agent

基于 LangGraph + LLM 的 Web3 支付代理，完整实现意图识别、服务发现、x402 协商式付费、风控/HITL、EIP-712 签名与收据验证，并通过 Facilitator 完成 verify/settle 交互。

## 系统架构
**顶层图（Agent 智能层）**
- `intent_router`（LLM）：判断是否需要付费调用
- `service_registry`（LLM + 工具）：发现服务并选择最优候选
- `normal_chat`：非付费任务直接回复
- `payment_subgraph`：支付闭环子图（见下方）

**支付子图（x402）**
- `request_executor` → `header_parser` → `payment_negotiator` → `risk_assessor`
- `human_approver`（interrupt/resume）：触发人在环路
- `payment_signer`（EIP-712 / EIP-3009）
- `response_validator`（PAYMENT-RESPONSE）
- `error_handler` + `reflection_rewriter`（失败反思与重试）

**Facilitator**
- `/verify`：结构化验签（EIP-712）
- `/settle`：模拟结算并返回收据

## 节点设计与职责
- `intent_router`：抽取意图，决定是否进入支付流程
- `service_registry`：通过工具发现服务、选择免费/付费路径
- `request_executor`：发送请求，处理 200/402/4xx/5xx
- `header_parser`：解析 PAYMENT-REQUIRED
- `payment_negotiator`：选择 accepts 中最合适方案
- `risk_assessor`：白名单/阈值/预算风控，决定 auto/HITL/reject
- `human_approver`：interrupt 等待人工确认
- `payment_signer`：构造并签名 EIP-712 载荷
- `response_validator`：解析 PAYMENT-RESPONSE 收据
- `error_handler`：失败分类与退避
- `reflection_rewriter`：失败反思与策略调整
- `normal_chat`：普通问答直接回复

## 工作流程（端到端）
1. `intent_router` 判断是否需要付费调用
2. `service_registry` 发现并选择服务
3. `request_executor` 探测请求：
   - 200：直接完成
   - 402：进入支付协商
4. `payment_negotiator` 选择 accepts 方案
5. `risk_assessor` 风控决策（auto/HITL/reject）
6. `payment_signer` 签名 EIP-712
7. `request_executor` 携带签名重试
8. `response_validator` 解析收据
9. 失败进入 `error_handler` + `reflection_rewriter` 重试或终止

## 目录结构
- `src/utils.py`：状态定义、reducers、服务注册表、钱包
- `src/nodes.py`：节点实现与日志写入
- `src/graph.py`：顶层图 + 支付子图
- `src/llm.py`：`create_agent` 结构化输出
- `src/mock_server.py`：x402 模拟服务端
- `src/facilitator_server.py`：Facilitator verify/settle
- `src/service_bootstrap.py`：自动启动服务
- `src/demo.py`：五个场景 demo + JSON/MD 日志

## Demo 场景
- `demo-1`：非支付意图 → normal_chat
- `demo-2`：免费服务路径（200，无支付）
- `demo-3`：小额自动支付（402 → sign → 200）
- `demo-4`：高额触发 HITL
- `demo-5`：一次验证失败 → 反思重试成功

日志输出：
- JSON：`logs/demo-*.json`
- Markdown（按节点顺序）：`logs/demo-*.md`

## 本地运行
- 创建虚拟环境：`python3 -m venv .venv`
- 安装依赖：`.venv/bin/pip install -r requirements.txt`
- 运行 demo：`.venv/bin/python src/demo.py demo-1`

## LangGraph Dev / LangSmith
- 安装：`pip install -U "langgraph-cli[inmem]"`
- 启动：`langgraph dev -c langgraph.json`

启动时会自动拉起 facilitator 与所有 mock 服务。  
LangSmith 运行会自动写入：
- `logs/langsmith-<thread_id>.md`

## LLM 配置
`.env`（位于 `src/.env`）：
- `OPENAI_API_KEY`
- `BASE_URL`（例如 `https://api.deepseek.com/v1`）
- `MODEL_NAME`

## Checkpointing
- 设置 `CHECKPOINT_DB_URL` 为 `sqlite:///checkpoints.db` 或 Postgres URL
- `enable_checkpoint=True` 时启用持久化

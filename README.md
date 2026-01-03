# x402 LangGraph Payment Agent

This is a scaffold for an automated trading/payment agent in a Web3 setting using the x402 protocol and LangGraph.

## Structure
- `src/utils.py`: state types, wallet interface, helpers
- `src/nodes.py`: graph nodes (request, parse, risk, approval, sign, validate, error)
- `src/graph.py`: graph construction and routing
- `src/workflow.py`: workflow execution entry
- `src/demo.py`: full end-to-end demo (mock server + agent flow)
- `src/mock_server.py`: minimal x402-like HTTP server (returns 402 then 200)
- `requirements.txt`: runtime dependencies

## Next steps (planned)
1. Replace `DummyWalletProvider` with a real wallet integration (e.g., AgentKit/CDP wallet).
2. Add configuration loading for network, token metadata, and facilitator endpoints.
3. Add request wrappers and mock server tests for 402/200 flows.

## Local dev
- Create venv: `python3 -m venv .venv`
- Install deps: `.venv/bin/pip install -r requirements.txt`
- Run mock server: `.venv/bin/python src/mock_server.py`
- Run demo: `.venv/bin/python src/demo.py`

## LangGraph dev
- Install CLI: `pip install -U "langgraph-cli[inmem]"`
- Start dev server: `langgraph dev -c langgraph.json`
- Run tests: `PYTHONPATH=src .venv/bin/python -m unittest tests/test_mock_flow.py tests/test_error_paths.py`

## AgentKit usage (optional)
Use `AgentKitWalletProvider.from_config({...})` or `build_wallet_provider_from_config(AgentConfig(...))` with `agentkit_api_key_name`, `agentkit_api_key_secret`, and `agentkit_network_id`.

## Preferences and checkpointing
- Preferences live in `AgentConfig` (`preferred_network`, `preferred_asset`, `preferred_scheme`).
- Enable `enable_checkpoint=True` to compile the graph with an in-memory checkpointer.

## LLM configuration
- `.env` supports `OPENAI_API_KEY`, `OPENAI_BASE_URL` (default `https://api.deepseek.com/v1`), and `MODEL_NAME`.
- Set `enable_llm=True` in `AgentConfig` to activate LLM intent/negotiation/reflection nodes.

## Checkpointing with DATABASE_URL
- Set `DATABASE_URL` to `sqlite:///checkpoints.db` or a Postgres URL.
- Enable `enable_checkpoint=True` to persist thread state via SqliteSaver/PostgresSaver.

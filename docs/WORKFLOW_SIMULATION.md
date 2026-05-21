# Workflow Simulation

## Example Loop

The checked-in fixtures under `examples/` are sanitized. They do not contain private local paths, ChatGPT URLs, thread IDs from real sessions, browser data, or secrets.

1. Render a decision against the sample context:

```bash
codex-bridge decide \
  --context examples/decision_context.json \
  --registry "$BRIDGE_HOME/state/THREAD_REGISTRY.json" \
  --write
```

2. Render a prompt from the sample request:

```bash
codex-bridge prompt \
  --request examples/prompt_request.json \
  --output "$BRIDGE_HOME/artifacts/NEXT_PROMPT.md"
```

3. Optionally run the prepared prompt through the local wrapper with the safe mock executable:

```bash
python3 examples/mock_codex_exec.py < "$BRIDGE_HOME/artifacts/NEXT_PROMPT.md"
```

4. Log the sanitized sample report:

```bash
codex-bridge log \
  --report examples/run_report.json \
  --log "$BRIDGE_HOME/logs/EXECUTION_LOG.md" \
  --registry "$BRIDGE_HOME/state/THREAD_REGISTRY.json"
```

## Expected Outcome

- runtime output is written under `BRIDGE_HOME`
- the source checkout remains clean
- examples stay deterministic and public-safe

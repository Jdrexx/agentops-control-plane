# Pinned mock responses

The built-in `mock` provider is deterministic: the same
`(model, system, prompt)` always produces the same output. For prompts you care
about, you can *pin* the exact response by dropping a file named after the
prompt's fingerprint in this directory.

## How it works

The provider computes `sha256(model + "\x1f" + system + "\x1f" + prompt)` and
looks for `<first 16 hex chars>.txt` in `AGENTOPS_MOCK_SCRIPT_DIR` (default:
`examples/mock_responses/`). If the file exists, its contents are returned
verbatim. Otherwise a stable synthetic filler is generated.

## Pinning a response

1. Compute the fingerprint for the exact step configuration you plan to use:

   ```bash
   cd <repo root>
   python -c "from src.agentops.providers import _mock_fingerprint; print(_mock_fingerprint('mock-small', 'You are a QA reviewer. Reply PASS or FAIL.', 'Review this reply for tone and accuracy:'))"
   ```

2. Write the desired response text to `<fingerprint>.txt` in this directory.

3. Run the workflow with `provider: mock` and the same model/system/prompt. The
   provider returns your pinned text — byte for byte.

## Conventions

- One pinned script per prompt configuration; name it after the fingerprint.
- Keep scripts short and synthetic. Never pin real customer data.
- A `<fingerprint>.fail` file makes the same prompt raise a deterministic
  `ProviderError` whose message is the file contents — offline failure
  injection for incident demos and retry-path tests.
- The same pinning works for `llm_judge` evaluation prompts, so an evaluation
  can be made to fail on workflow v1 and pass on v2 in a demo.
- Set `AGENTOPS_MOCK_CHUNK_MS=35` for demos (the live stream panel visibly
  types) and `AGENTOPS_MOCK_CHUNK_MS=0` in CI (instant tests).

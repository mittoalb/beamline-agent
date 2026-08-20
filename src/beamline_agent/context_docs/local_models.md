# Local AI models — Agent Context

Röntgen can talk to a **locally-hosted** LLM instead of (or in
addition to) a cloud API. Any server that exposes the OpenAI
`/v1/chat/completions` protocol works — Ollama, llama.cpp server,
vLLM, LM Studio, LocalAI, and most other modern runners do. The
chat loop, tool-use plumbing, and history persistence are identical
to the cloud path; only the URL, the API-key convention, and the
inference latency change.

## Quick setup

1. Open the AI Agent panel's **⚙ Settings** dialog.
2. Set **Backend** = "Local — OpenAI-compatible".
3. Fill in **Base URL** with your runner's endpoint (see the
   reference table below or the fill-in checklist further down).
4. Leave **API key** blank — a placeholder value is sent
   automatically. (If your specific setup does require a key, put
   it here.)
5. Click **Connect / refresh models** — the dropdown should
   populate with whatever models the server is serving.
6. Pick one, close the dialog, and start chatting.

## Fill-in checklist (edit me when you set up your local runner)

The user maintains this section — fill in the specifics for the
runner + host actually in use so future turns of Röntgen have the
concrete facts.

- **Runner**: (Ollama / llama.cpp / vLLM / LM Studio / other — )
- **Host**: (localhost / tomo2 / gauss / … — )
- **Port**: (default varies — see reference table — )
- **Full base URL**: (e.g. `http://tomo2:11434/v1` — )
- **Model name(s)** as the runner reports them: (e.g.
  `llama3.1:70b-instruct-q4_K_M` — )
- **API key required?** (y/n; most local runners: no — )
- **SSH tunnel needed?** (if the port isn't exposed to the pystream
  host; e.g. `ssh -N -L 11434:localhost:11434 tomo2` — )
- **Firewall / EPICS_CA_ADDR_LIST notes**: (if the runner host has
  a restricted network — )
- **Startup command / systemd unit**: (how the user (re)starts the
  runner — )

## Reference table — common runners

| Runner       | Default URL                  | Tool-use notes                            |
|--------------|------------------------------|-------------------------------------------|
| Ollama       | `http://<host>:11434/v1`     | Works with modern models (llama3.1+, qwen2.5+, mistral-nemo+). Older models silently drop tool calls. |
| llama.cpp    | `http://<host>:8080/v1`      | Depends on the GGUF; check the model card for `function-calling` / `tools` support.                    |
| vLLM         | `http://<host>:8000/v1`      | Best tool-use fidelity of the local runners; needs `--enable-auto-tool-choice` flag on some models.    |
| LM Studio    | `http://<host>:1234/v1`      | Recent builds handle tools; older ones don't. GUI settings expose an "Enable tools" toggle.            |
| LocalAI      | `http://<host>:8080/v1`      | Same protocol; capability depends on backend model.                                                    |

All of these speak the OpenAI Chat Completions protocol. Röntgen's
"Backend = Local — OpenAI-compatible" preset is just PROTOCOL_OPENAI
with a friendlier URL placeholder and an optional API-key field.

## When to use local vs. cloud

- **Cloud (Anthropic / OpenAI gateway)** — best model quality,
  reliable tool-use, needs internet + credit. Use when you want
  Claude Sonnet 5 / GPT-5 level reasoning.
- **Local** — data stays on-site, no per-token cost, works
  offline. Slower on CPU; comparable to cloud on a decent GPU
  host. Tool-use fidelity varies by model — some local models
  silently drop tool calls or hallucinate arguments. Pick a
  model known to handle function calling well (llama3.1 70B,
  qwen2.5-coder 32B, mistral-nemo, etc.).

## When to switch

- Set-up debug work + reasoning-heavy tasks (physicist derivations,
  complex multi-step operator sequences, root-causing a bug in
  pystream) → cloud, until a local model proves it can keep up.
- Routine reads + repetitive procedures (motor moves, PV checks,
  layout enumeration, status-page fetches) → local, since the
  tool-use surface is narrow and the extra latency doesn't matter.
- Anything that touches user-sensitive raw data you don't want
  sent to a cloud API → local, always.

## Known gotchas (append as you find them)

- Empty API key + strict server → some hosted OpenAI-compat
  proxies check for a bearer token even when they don't use it.
  If Connect returns 401 with a blank field, put any non-empty
  string in the API-key field (e.g. `local`).
- Tool arguments as strings vs objects → some local models pass
  tool arguments as a JSON string when the spec expects an object.
  The current `_chat_openai` implementation `json.loads`es
  arguments — should tolerate both.
- Model-listing returns nothing → check the runner's own
  `/v1/models` endpoint with `curl http://<host>:<port>/v1/models`
  first. Ollama needs a model pulled (`ollama pull <name>`) before
  it appears; vLLM lists only the model it was started with.
- Slower first token → local models often have a longer TTFT;
  bump the chat timeout in `chat.py::_chat_openai` from 60s if
  needed (or `--timeout` when starting the runner).

## Not in scope

- Auto-discovery of running local servers on startup (`nc` to
  common ports). Skipped for now; add if it becomes routine.
- GPU-utilisation monitoring (nvidia-smi, DCGM). Not this doc's
  job — the beamline has separate infra for that.
- A bespoke `PROTOCOL_LOCAL` — every practical local runner is
  OpenAI-compatible, so local rides `PROTOCOL_OPENAI`. Add if some
  future runner needs its own wire format.

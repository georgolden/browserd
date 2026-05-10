# Provider/Model Selection Layer for BrowserD

**Goal:** Replace hardcoded `ChatDeepSeek` with a configurable provider+model system so anyone can use BrowserD with their preferred LLM. Add an interactive `browser-cli setup` command that walks through provider → model → API key.

**Status:** planned, not started

---

## Current State

- `tasks.py` line 305: `from browser_use.llm import ChatDeepSeek` (hardcoded)
- `tasks.py` line 408-411: `ChatDeepSeek(model=t["model"], api_key=...)`  
- `models.py`: `DaemonConfig` has only `deepseek_api_key: str | None` and `default_model: str = "deepseek-chat"`
- `daemon.py` line 220: only loads `DEEPSEEK_API_KEY`

## What browser-use Supports

17 providers, 12 top-level exported:

| Provider | Key Env Var | Top-Level Class |
|----------|------------|-----------------|
| DeepSeek | `DEEPSEEK_API_KEY` | `ChatDeepSeek` |
| OpenAI | `OPENAI_API_KEY` | `ChatOpenAI` |
| Anthropic | `ANTHROPIC_API_KEY` | `ChatAnthropic` |
| Google | `GOOGLE_API_KEY` | `ChatGoogle` |
| Browser Use | `BROWSER_USE_API_KEY` | `ChatBrowserUse` |
| Groq | `GROQ_API_KEY` | `ChatGroq` |
| Mistral | `MISTRAL_API_KEY` | `ChatMistral` |
| Azure OpenAI | `AZURE_OPENAI_API_KEY` + endpoint | `ChatAzureOpenAI` |
| Cerebras | `CEREBRAS_API_KEY` | `ChatCerebras` |
| Ollama | none (local) | `ChatOllama` |
| LiteLLM | per-provider | `ChatLiteLLM` |
| Vercel AI Gateway | `AI_GATEWAY_API_KEY` | `ChatVercel` |
| OpenRouter | `OPENROUTER_API_KEY` | `ChatOpenRouter`* |
| AWS Bedrock | AWS creds | `ChatAWSBedrock`* |
| OCI | OCI config | `ChatOCIRaw` |

*not top-level exported, need full module path

## Proposed Approach

### 1. Provider Registry (`browserd/providers.py` — new file)

A data structure mapping provider IDs to metadata:

```python
PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "chat_class": "ChatDeepSeek",
        "import_path": "browser_use.llm.deepseek.chat",
        "env_vars": ["DEEPSEEK_API_KEY"],
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "openai": { ... },
    "anthropic": { ... },
    "google": { ... },
    "browser-use": { ... },
    ...
}
```

### 2. Config Updates (`models.py`)

Add to `DaemonConfig`:
- `llm_provider: str = "deepseek"` — provider ID from registry
- `llm_model: str | None = None` — overrides provider's default model
- `deepseek_api_key` stays for backward compat but `llm_provider` wins when set

Remove: `default_model` (replaced by provider defaults)

### 3. Dynamic LLM Construction (`tasks.py`)

Replace hardcoded import with a factory function:

```python
def _build_llm(config: DaemonConfig, model_override: str | None = None):
    provider = get_provider(config.llm_provider)
    api_key = resolve_api_key(provider)
    chat_cls = import_chat_class(provider)
    model = model_override or config.llm_model or provider.default_model
    return chat_cls(model=model, api_key=api_key)  # or kwargs per provider
```

API key resolution: check env var from registry, fall back to config field.

### 4. Interactive Setup (`browser-cli setup` — new command)

Steps:
```
1. Show list of available providers (numbered)
2. User picks one → stores LLM_PROVIDER=<id>
3. Show default model + common alternatives for that provider
4. User enters model name (or accepts default) → stores LLM_MODEL
5. Prompt for API key (hidden input) → stores <PROVIDER_API_KEY_ENV_VAR>
6. Write all to ~/.browserd/.env
7. Print: "Run browser-cli daemon restart to apply"
```

Edge cases:
- Ollama: skip API key prompt, ask for base_url instead
- Azure: need endpoint URL in addition to key
- AWS/OCI: need region/profile, not just key

### 5. Env Var Changes

New env vars in `~/.browserd/.env`:
```
LLM_PROVIDER=deepseek          # provider ID
LLM_MODEL=deepseek-chat        # optional, defaults to provider default
DEEPSEEK_API_KEY=sk-...        # provider-specific key (existing)
```

Note: The per-provider API keys use the same env var names that browser-use expects natively — no translation layer needed since we set them in the shell environment before browser-use reads them.

## Files to Change

| File | What |
|------|------|
| `browserd/providers.py` | NEW — provider registry |
| `browserd/models.py` | Add `llm_provider`, `llm_model` fields |
| `browserd/tasks.py` | Replace hardcoded ChatDeepSeek with dynamic factory |
| `browserd/daemon.py` | Load `LLM_PROVIDER`/`LLM_MODEL` from env |
| `browserd/cli.py` | Add `setup` and `setup provider` commands |
| `README.md` | Update with provider config docs |

## Implementation Order

1. Create `providers.py` with registry
2. Update `models.py` config fields
3. Build `_build_llm` factory in `tasks.py`
4. Update `daemon.py` env loading
5. Add `browser-cli setup` interactive command
6. Test: run a task with DeepSeek (default, backward compat)
7. Test: switch to another provider via env vars, run a task
8. Update README

## Risks / Open Questions

- **Constructor signatures vary** — some need `api_key`, some auto-detect, some need `base_url`, Azure needs `azure_endpoint` + `api_version`. The factory needs a per-provider kwargs builder.
- **Ollama (local)** — no API key. Need `base_url` param. Different enough to flag as "advanced" in setup.
- **AWS/OCI** — require config files, not just env vars. Skip from MVP setup wizard, support via manual env.
- **OpenRouter** — not top-level exported from browser-use. Need full import path `browser_use.llm.openrouter.chat`.
- **Default model per provider** — can be hardcoded in registry, but models change frequently. Consider a `browser-cli setup models` command that lists known models for a provider.

## Verification

- Run `browser-cli setup` → pick OpenAI → enter key → `LLM_PROVIDER=openai` + `OPENAI_API_KEY=sk-...` written to `.env`
- Restart daemon → `browser-cli run "go to example.com"` → uses ChatOpenAI, task completes
- Switch to Anthropic via `browser-cli set setenv LLM_PROVIDER anthropic` + key → restart → uses ChatAnthropic
- Backward compat: no `LLM_PROVIDER` set → defaults to `deepseek` → existing behavior preserved

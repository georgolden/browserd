"""LLM provider registry for BrowserD — maps provider IDs to browser-use chat classes.

Each provider entry defines the import path, API key env vars, default model,
and known model choices for the interactive setup wizard.
"""

from __future__ import annotations

from typing import Any

# ── Provider definitions ────────────────────────────────────────────────────
# provider_id: unique key used in LLM_PROVIDER env var
# name: display name for CLI menus
# chat_class: class name in browser-use
# import_path: dotted module path for lazy import
# env_vars: list of env var names needed for API key (first is primary)
# default_model: model used when LLM_MODEL is not set
# models: list of commonly-used models for the setup wizard to suggest
# extra_prompts: optional list of extra fields to prompt for during setup
#   each: (env_var, prompt_label, default_value)
# constructor_kwargs_override: if set, these kwargs replace api_key in constructor

PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "name": "DeepSeek",
        "chat_class": "ChatDeepSeek",
        "import_path": "browser_use.llm.deepseek.chat",
        "attr": "ChatDeepSeek",
        "env_vars": ["DEEPSEEK_API_KEY"],
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "openai": {
        "name": "OpenAI",
        "chat_class": "ChatOpenAI",
        "import_path": "browser_use.llm.openai.chat",
        "attr": "ChatOpenAI",
        "env_vars": ["OPENAI_API_KEY"],
        "default_model": "gpt-4.1-mini",
        "models": [
            "gpt-4.1-mini", "gpt-4o", "gpt-4.1", "o3", "o4-mini",
            "gpt-5", "gpt-5-nano", "gpt-5.1-codex",
        ],
    },
    "anthropic": {
        "name": "Anthropic",
        "chat_class": "ChatAnthropic",
        "import_path": "browser_use.llm.anthropic.chat",
        "attr": "ChatAnthropic",
        "env_vars": ["ANTHROPIC_API_KEY"],
        "default_model": "claude-sonnet-4-0",
        "models": [
            "claude-sonnet-4-0", "claude-3.5-sonnet",
            "claude-3-opus", "claude-3-haiku",
        ],
    },
    "google": {
        "name": "Google Gemini",
        "chat_class": "ChatGoogle",
        "import_path": "browser_use.llm.google.chat",
        "attr": "ChatGoogle",
        "env_vars": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
        "default_model": "gemini-2.5-flash",
        "models": [
            "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite",
            "gemini-3-pro-preview", "gemini-flash-latest",
        ],
    },
    "browser-use": {
        "name": "Browser Use (Cloud)",
        "chat_class": "ChatBrowserUse",
        "import_path": "browser_use.llm.browser_use.chat",
        "attr": "ChatBrowserUse",
        "env_vars": ["BROWSER_USE_API_KEY"],
        "default_model": "bu-latest",
        "models": ["bu-latest", "bu-1-0", "bu-2-0", "browser-use/bu-30b-a3b-preview"],
    },
    "groq": {
        "name": "Groq",
        "chat_class": "ChatGroq",
        "import_path": "browser_use.llm.groq.chat",
        "attr": "ChatGroq",
        "env_vars": ["GROQ_API_KEY"],
        "default_model": "meta-llama/llama-4-maverick-17b-128e-instruct",
        "models": [
            "meta-llama/llama-4-maverick-17b-128e-instruct",
            "qwen/qwen3-32b",
            "moonshotai/kimi-k2-instruct",
        ],
    },
    "mistral": {
        "name": "Mistral",
        "chat_class": "ChatMistral",
        "import_path": "browser_use.llm.mistral.chat",
        "attr": "ChatMistral",
        "env_vars": ["MISTRAL_API_KEY"],
        "default_model": "mistral-large-latest",
        "models": [
            "mistral-large-latest", "mistral-medium-latest",
            "mistral-small-latest", "codestral-latest", "pixtral-large-latest",
        ],
    },
    "azure": {
        "name": "Azure OpenAI",
        "chat_class": "ChatAzureOpenAI",
        "import_path": "browser_use.llm.azure.chat",
        "attr": "ChatAzureOpenAI",
        "env_vars": ["AZURE_OPENAI_API_KEY"],
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4.1-mini", "gpt-5.1-codex", "gpt-5-codex"],
        "extra_prompts": [
            ("AZURE_OPENAI_ENDPOINT", "Azure endpoint URL", "https://<your-resource>.openai.azure.com"),
        ],
    },
    "cerebras": {
        "name": "Cerebras",
        "chat_class": "ChatCerebras",
        "import_path": "browser_use.llm.cerebras.chat",
        "attr": "ChatCerebras",
        "env_vars": ["CEREBRAS_API_KEY"],
        "default_model": "llama-3.3-70b",
        "models": [
            "llama-3.3-70b", "llama3.1-8b",
            "qwen-3-32b", "qwen-3-coder-480b",
        ],
    },
    "ollama": {
        "name": "Ollama (local)",
        "chat_class": "ChatOllama",
        "import_path": "browser_use.llm.ollama.chat",
        "attr": "ChatOllama",
        "env_vars": [],  # no API key needed
        "default_model": "llama3",
        "models": [],  # depends on what user has pulled
        "no_api_key": True,
        "extra_prompts": [
            ("OLLAMA_BASE_URL", "Ollama base URL", "http://localhost:11434"),
        ],
    },
    "openrouter": {
        "name": "OpenRouter",
        "chat_class": "ChatOpenRouter",
        "import_path": "browser_use.llm.openrouter.chat",
        "attr": "ChatOpenRouter",
        "env_vars": ["OPENROUTER_API_KEY"],
        "default_model": "openai/gpt-4o",
        "models": [
            "openai/gpt-4o", "anthropic/claude-3.5-sonnet",
            "google/gemini-2.5-flash", "deepseek/deepseek-chat",
        ],
    },
    "vercel": {
        "name": "Vercel AI Gateway",
        "chat_class": "ChatVercel",
        "import_path": "browser_use.llm.vercel.chat",
        "attr": "ChatVercel",
        "env_vars": ["AI_GATEWAY_API_KEY"],
        "default_model": "anthropic/claude-3.5-sonnet",
        "models": [
            "anthropic/claude-3.5-sonnet", "alibaba/qwen-3-32b",
            "meta-llama/llama-3.3-70b", "openai/gpt-4o",
        ],
    },
    "litellm": {
        "name": "LiteLLM (proxy)",
        "chat_class": "ChatLiteLLM",
        "import_path": "browser_use.llm.litellm.chat",
        "attr": "ChatLiteLLM",
        "env_vars": [],  # per-deployment
        "default_model": "openai/gpt-4o",
        "models": [],
        "no_api_key": True,
        "extra_prompts": [
            ("LITELLM_BASE_URL", "LiteLLM proxy URL", "http://localhost:4000"),
        ],
    },
}


def get_provider(provider_id: str) -> dict[str, Any]:
    """Look up a provider by ID. Raises KeyError if not found."""
    return PROVIDERS[provider_id]


def list_providers() -> list[dict[str, Any]]:
    """Return all providers as a list for display in setup UI."""
    return [
        {"id": pid, "name": p["name"], "default_model": p["default_model"]}
        for pid, p in PROVIDERS.items()
    ]

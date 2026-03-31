"""Model discovery, family detection, recommended models, and autocomplete.

Uses litellm.model_cost as the source of truth for available models.
Filters to chat-capable models with tool-calling support (required by Holmes).
Groups models into families and auto-detects the latest version per family.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import litellm
import yaml

from holmes.core.config import config_path_dir

# Env var names checked per provider (used for display)
PROVIDER_ENV_VARS: Dict[str, List[str]] = {
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    "azure": ["AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION"],
    "bedrock": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "ollama": ["OLLAMA_API_BASE"],
}

# Model families we curate recommendations for.
# Each entry: (provider, tier_name, name_pattern, sort_key_extractor)
# The "alias" models (no date suffix) are preferred as they track the latest.
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")  # e.g. -20260205

# Families defined as (litellm_provider, display_tier, regex for alias models)
# We match alias models (no date suffix) and pick the highest version.
FAMILIES = [
    # Anthropic — minor version is 1-2 digits to exclude date suffixes (YYYYMMDD)
    ("anthropic", "Opus", re.compile(r"^claude-opus-(\d+)-?(\d{1,2})?$")),
    ("anthropic", "Sonnet", re.compile(r"^claude-sonnet-(\d+)-?(\d{1,2})?$")),
    ("anthropic", "Haiku", re.compile(r"^claude-haiku-(\d+)-?(\d{1,2})?$")),
    # OpenAI
    ("openai", "GPT", re.compile(r"^gpt-(\d+)\.?(\d+)?$")),
    ("openai", "GPT Mini", re.compile(r"^gpt-(\d+)\.?(\d+)?-mini$")),
    ("openai", "GPT Nano", re.compile(r"^gpt-(\d+)\.?(\d+)?-nano$")),
    ("openai", "o-series", re.compile(r"^o(\d+)$")),
    ("openai", "o-series Mini", re.compile(r"^o(\d+)-mini$")),
    # Gemini
    ("gemini", "Gemini Pro", re.compile(r"^gemini/gemini-(\d+)\.?(\d+)?-pro$")),
    ("gemini", "Gemini Flash", re.compile(r"^gemini/gemini-(\d+)\.?(\d+)?-flash$")),
    # DeepSeek
    ("deepseek", "DeepSeek", re.compile(r"^deepseek/deepseek-v?(\d+)\.?(\d+)?$")),
]


@dataclass
class RecommendedModel:
    """A recommended model with metadata for display."""

    model_name: str  # litellm model identifier, e.g. "claude-opus-4-6"
    provider: str  # litellm_provider value
    tier: str  # e.g. "Opus", "GPT", "Gemini Pro"
    context_tokens: int = 0
    input_cost_per_mtok: float = 0.0  # cost per million input tokens
    supports_reasoning: bool = False
    missing_keys: List[str] = field(default_factory=list)

    @property
    def is_configured(self) -> bool:
        return len(self.missing_keys) == 0

    @property
    def provider_display(self) -> str:
        return self.provider.capitalize()

    @property
    def context_display(self) -> str:
        if self.context_tokens >= 1_000_000:
            val = self.context_tokens / 1_000_000
            return f"{val:g}M"
        if self.context_tokens >= 1_000:
            val = self.context_tokens / 1_000
            return f"{val:g}K"
        return str(self.context_tokens)

    @property
    def cost_display(self) -> str:
        if self.input_cost_per_mtok == 0:
            return "free"
        return f"${self.input_cost_per_mtok:.2f}/M in"


def _get_tool_calling_chat_models() -> Dict[str, dict]:
    """Return all chat models with tool-calling support from litellm."""
    return {
        k: v
        for k, v in litellm.model_cost.items()
        if v.get("mode") == "chat" and v.get("supports_function_calling")
    }


def _extract_version_key(match: re.Match) -> Tuple[int, int]:
    """Extract (major, minor) version from regex match groups."""
    major = int(match.group(1)) if match.group(1) else 0
    minor = int(match.group(2)) if match.lastindex and match.lastindex >= 2 and match.group(2) else 0
    return (major, minor)


def get_recommended_models() -> List[RecommendedModel]:
    """Auto-detect the latest model per family from litellm's catalog.

    Returns models sorted by: configured first, then by provider/tier.
    """
    chat_models = _get_tool_calling_chat_models()
    results: List[RecommendedModel] = []

    for provider, tier, pattern in FAMILIES:
        best_name: Optional[str] = None
        best_version: Tuple[int, int] = (-1, -1)

        for model_name, model_info in chat_models.items():
            if model_info.get("litellm_provider") != provider:
                continue
            match = pattern.match(model_name)
            if not match:
                continue
            version = _extract_version_key(match)
            if version > best_version:
                best_version = version
                best_name = model_name

        if best_name is None:
            continue

        info = chat_models[best_name]

        # Check prerequisites via litellm
        try:
            env_check = litellm.validate_environment(best_name)
            missing = env_check.get("missing_keys", [])
        except Exception:
            missing = []

        results.append(
            RecommendedModel(
                model_name=best_name,
                provider=provider,
                tier=tier,
                context_tokens=info.get("max_input_tokens", 0),
                input_cost_per_mtok=info.get("input_cost_per_token", 0) * 1_000_000,
                supports_reasoning=info.get("supports_reasoning", False),
                missing_keys=missing,
            )
        )

    # Sort: configured models first, then by provider name, then tier
    results.sort(key=lambda m: (not m.is_configured, m.provider, m.tier))
    return results


def get_detected_providers() -> Dict[str, bool]:
    """Return a dict of provider -> has_api_key for common providers."""
    detected = {}
    for provider, env_vars in PROVIDER_ENV_VARS.items():
        # Provider is "detected" if at least one of its env vars is set
        detected[provider] = any(os.environ.get(v) for v in env_vars)
    return detected


def get_model_completions(prefix: str) -> List[str]:
    """Return model names matching a prefix, for autocomplete.

    Filters to chat + tool-calling models only. Returns up to 50 matches.
    """
    prefix_lower = prefix.lower()
    chat_models = _get_tool_calling_chat_models()
    matches = [k for k in sorted(chat_models.keys()) if k.lower().startswith(prefix_lower)]
    return matches[:50]


def check_model_prerequisites(model: str) -> Dict:
    """Check what a model needs to be configured.

    Returns dict with 'keys_in_environment' and 'missing_keys'.
    """
    try:
        return litellm.validate_environment(model)
    except Exception as e:
        return {"keys_in_environment": False, "missing_keys": [str(e)]}


def get_model_provider(model: str) -> Optional[str]:
    """Get the provider for a model string."""
    try:
        result = litellm.get_llm_provider(model)
        return result[1] if result else None
    except Exception:
        return None


def is_known_model(model: str) -> bool:
    """Check if a model name is known to litellm."""
    try:
        litellm.get_llm_provider(model)
        return True
    except Exception:
        return False


def save_model_to_config(
    config_file_path: Path,
    model: str,
    api_key: Optional[str] = None,
) -> Tuple[bool, str]:
    """Save model (and optionally api_key) to the Holmes config file.

    Merges into existing config without overwriting other fields.
    Returns (success, message).
    """
    config_file = Path(config_file_path)
    existing: Dict = {}
    if config_file.exists():
        try:
            with open(config_file, "r") as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            existing = {}

    existing["model"] = model
    if api_key:
        existing["api_key"] = api_key

    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w") as f:
            yaml.dump(existing, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        return False, f"Failed to write {config_file}: {e}"

    return True, f"Model saved to {config_file}"


def get_env_var_for_api_key(provider: str) -> Optional[str]:
    """Return the primary env var name for a provider's API key."""
    env_vars = PROVIDER_ENV_VARS.get(provider, [])
    # Return the first one that looks like an API key (not a URL or version)
    for v in env_vars:
        if "KEY" in v or "TOKEN" in v or "SECRET" in v:
            return v
    return env_vars[0] if env_vars else None

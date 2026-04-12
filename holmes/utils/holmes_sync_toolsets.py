import json
import yaml
import logging
from datetime import datetime
from typing import Any, List

from holmes.config import Config
from holmes.core.supabase_dal import SupabaseDal
from holmes.core.tools import Toolset, ToolsetDBModel
from holmes.plugins.prompts import load_and_render_prompt
from holmes.plugins.toolsets.mcp.toolset_mcp import RemoteMCPToolset


def log_toolsets_statuses(toolsets: List[Toolset]):
    enabled_toolsets = [
        toolset.name for toolset in toolsets if toolset.status.value == "enabled"
    ]
    disabled_toolsets = [
        toolset.name for toolset in toolsets if toolset.status.value != "enabled"
    ]
    logging.info(f"Enabled toolsets: {enabled_toolsets}")
    logging.info(f"Disabled toolsets: {disabled_toolsets}")


def holmes_sync_toolsets_status(dal: SupabaseDal, config: Config) -> None:
    """
    Method for synchronizing toolsets with the database:
    1) Fetch all built-in toolsets from the holmes/plugins/toolsets directory
    2) Load custom toolsets defined in /etc/holmes/config/custom_toolset.yaml
    3) Override default toolsets with corresponding custom configurations
       and add any new custom toolsets that are not part of the defaults
    4) Run the check_prerequisites method for each toolset
    5) Use sync_toolsets to upsert toolset's status and remove toolsets that are not loaded from configs or folder with default directory
    """
    tool_executor = config.create_tool_executor(dal)

    if not config.cluster_name:
        raise Exception(
            "Cluster name is missing in the configuration. Please ensure 'CLUSTER_NAME' is defined in the environment variables, "
            "or verify that a cluster name is provided in the Robusta configuration file."
        )

    db_toolsets = []
    updated_at = datetime.now().isoformat()
    for toolset in tool_executor.toolsets:
        # hiding disabled experimental toolsets from the docs
        if toolset.experimental and not toolset.enabled:
            continue

        meta = get_config_meta_for_toolset(toolset)
        if not toolset.installation_instructions:
            instructions = get_config_schema_for_toolset(toolset)
            toolset.installation_instructions = instructions
        db_toolsets.append(
            ToolsetDBModel(
                toolset_name=toolset.name,
                cluster_id=config.cluster_name,
                account_id=dal.account_id,
                updated_at=updated_at,
                icon_url=toolset.icon_url,
                status=toolset.status.value if toolset.status else None,
                error=toolset.error,
                description=toolset.description,
                docs_url=toolset.docs_url,
                installation_instructions=toolset.installation_instructions,
                meta=meta,
            ).model_dump()
        )
    dal.sync_toolsets(db_toolsets, config.cluster_name)
    log_toolsets_statuses(tool_executor.toolsets)


def get_config_meta_for_toolset(toolset: Toolset) -> dict | None:
    if not isinstance(toolset, RemoteMCPToolset):
        return None
    # For MCP toolsets, extract oauth config from installation_instructions if present
    if toolset.installation_instructions:
        try:
            parsed = json.loads(toolset.installation_instructions)
            if isinstance(parsed, dict) and "oauth" in parsed:
                return {"oauth_config": parsed["oauth"]}
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def get_config_schema_for_toolset(toolset: Toolset) -> str:
    res: dict = {
        "example_yaml": render_default_installation_instructions_for_toolset(toolset),
        "schema": toolset.get_config_schema(),
    }
    # Add oauth info for MCP toolsets that require OAuth authentication
    mcp_config = getattr(toolset, '_mcp_config', None)
    if mcp_config and hasattr(mcp_config, 'oauth') and mcp_config.oauth and mcp_config.oauth.enabled:
        oauth_config = mcp_config.oauth
        res["oauth"] = {
            "enabled": True,
            "authorization_url": oauth_config.authorization_url,
            "token_url": oauth_config.token_url,
            "client_id": oauth_config.client_id,
            "scopes": oauth_config.scopes,
            "registration_endpoint": oauth_config.registration_endpoint,
        }
    return json.dumps(res)

def render_default_installation_instructions_for_toolset(toolset: Toolset) -> str:
    env_vars = toolset.get_environment_variables()
    context: dict[str, Any] = {
        "env_vars": env_vars if env_vars else [],
        "toolset_name": toolset.name,
    }

    example_config = toolset.get_config_example()
    if example_config:
        context["example_config"] = yaml.dump(example_config)

    installation_instructions = load_and_render_prompt(
        "file://holmes/utils/default_toolset_installation_guide.jinja2", context
    )
    return installation_instructions

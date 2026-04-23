from abc import ABC
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Type, cast
from urllib.parse import urljoin

import requests  # type: ignore
from pydantic import Field

from holmes.core.tools import (
    CallablePrerequisite,
    StructuredToolResult,
    StructuredToolResultStatus,
    Tool,
    ToolInvokeContext,
    ToolParameter,
    Toolset,
)
from holmes.plugins.toolsets.utils import toolset_name_for_one_liner
from holmes.utils.pydantic_utils import ToolsetConfig


class TeamsConfig(ToolsetConfig):
    """Configuration for Microsoft Teams via delegated Microsoft Graph API.

    Authentication uses a pre-minted delegated Graph access token (Bearer).
    The token must carry these delegated scopes:
        User.Read
        User.ReadBasic.All
        Chat.ReadWrite
        ChatMessage.Send

    Example configuration:
    ```yaml
    toolsets:
      teams:
        enabled: true
        config:
          auth_token: "{{ env.TEAMS_AUTH_TOKEN }}"
    ```
    """

    auth_token: str = Field(
        title="Auth Token",
        description=(
            "Microsoft Graph delegated access token (Bearer). "
            "Mint via az CLI, MSAL, or any MCP/tool, and pass as env var."
        ),
    )
    graph_base_url: str = Field(
        default="https://graph.microsoft.com/v1.0",
        title="Graph Base URL",
        description="Microsoft Graph API base URL. Override only for sovereign clouds.",
    )


class TeamsToolset(Toolset):
    config_classes: ClassVar[list[Type[TeamsConfig]]] = [TeamsConfig]

    def __init__(self):
        super().__init__(
            name="teams",
            description=(
                "Microsoft Teams chat operations via delegated Graph API: "
                "search users, create a group chat, post messages, read chat history."
            ),
            icon_url="https://cdn.simpleicons.org/microsoftteams/6264A7",
            docs_url="https://holmesgpt.dev/data-sources/builtin-toolsets/teams/",
            prerequisites=[CallablePrerequisite(callable=self.prerequisites_callable)],
            tools=[
                TeamsSearchUsers(self),
                TeamsCreateChat(self),
                TeamsSendChatMessage(self),
                TeamsListChats(self),
                TeamsGetChatMessages(self),
            ],
        )

    def prerequisites_callable(self, config: dict[str, Any]) -> Tuple[bool, str]:
        try:
            self.config = TeamsConfig(**config)
        except Exception as e:
            return False, f"Invalid Teams toolset configuration: {e}"

        try:
            data, _ = self._graph_request("GET", "/me", timeout=10)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            text = e.response.text if e.response is not None else str(e)
            return (
                False,
                f"Microsoft Graph /me returned HTTP {status}: {text}. "
                "Check that auth_token is valid and has at least the User.Read scope.",
            )
        except requests.exceptions.RequestException as e:
            return False, f"Failed to reach Microsoft Graph: {e}"

        upn = data.get("userPrincipalName") or data.get("mail") or "unknown"
        return True, f"Teams toolset authenticated as {upn}"

    @property
    def teams_config(self) -> TeamsConfig:
        return cast(TeamsConfig, self.config)

    def _graph_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        url = urljoin(
            self.teams_config.graph_base_url.rstrip("/") + "/", endpoint.lstrip("/")
        )
        headers = {
            "Authorization": f"Bearer {self.teams_config.auth_token}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = requests.request(
            method, url, headers=headers, params=params, json=json_body, timeout=timeout
        )
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {}, dict(resp.headers)
        return resp.json(), dict(resp.headers)


class BaseTeamsTool(Tool, ABC):
    """Base class for Teams tools with shared Graph request and error handling."""

    def __init__(self, toolset: TeamsToolset, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._toolset = toolset

    def _graph_request(
        self,
        method: str,
        endpoint: str,
        params_for_result: Dict[str, Any],
        query: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> StructuredToolResult:
        try:
            data, _ = self._toolset._graph_request(
                method, endpoint, params=query, json_body=json_body
            )
            return StructuredToolResult(
                status=StructuredToolResultStatus.SUCCESS,
                data=data,
                params=params_for_result,
            )
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            body = e.response.text if e.response is not None else str(e)
            graph_err = self._extract_graph_error(body)
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=(
                    f"Graph API {method} {endpoint} failed with HTTP {status}. "
                    f"Query params: {query}. Request body: {json_body}. "
                    f"Graph error: {graph_err}. Raw response: {body}"
                ),
                params=params_for_result,
            )
        except requests.exceptions.RequestException as e:
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=f"Graph API {method} {endpoint} network error: {e}",
                params=params_for_result,
            )

    @staticmethod
    def _extract_graph_error(body: str) -> str:
        try:
            import json

            parsed = json.loads(body)
            err = parsed.get("error", {})
            return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
        except Exception:
            return "(unparseable error body)"


class TeamsSearchUsers(BaseTeamsTool):
    def __init__(self, toolset: TeamsToolset):
        super().__init__(
            toolset=toolset,
            name="teams_search_users",
            description=(
                "Search users in the Microsoft Entra directory by display name or email prefix. "
                "Returns a list of users with id, displayName, userPrincipalName, and mail. "
                "Use this to look up Microsoft Graph user IDs for adding to chats."
            ),
            parameters={
                "query": ToolParameter(
                    description="Prefix of the user's display name or email to match.",
                    type="string",
                    required=True,
                ),
                "limit": ToolParameter(
                    description="Maximum number of users to return (default 10, max 50).",
                    type="integer",
                    required=False,
                ),
            },
        )

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        query = params["query"].replace("'", "''")
        limit = min(int(params.get("limit") or 10), 50)
        filter_expr = (
            f"startsWith(displayName,'{query}') or startsWith(mail,'{query}') "
            f"or startsWith(userPrincipalName,'{query}')"
        )
        return self._graph_request(
            "GET",
            "/users",
            params_for_result=params,
            query={
                "$filter": filter_expr,
                "$select": "id,displayName,userPrincipalName,mail",
                "$top": limit,
            },
        )

    def get_parameterized_one_liner(self, params: Dict) -> str:
        return (
            f"{toolset_name_for_one_liner(self._toolset.name)}: "
            f"search users matching '{params.get('query', '')}'"
        )


class TeamsCreateChat(BaseTeamsTool):
    def __init__(self, toolset: TeamsToolset):
        super().__init__(
            toolset=toolset,
            name="teams_create_chat",
            description=(
                "Create a new Microsoft Teams chat. chat_type must be 'group' (3+ members) or "
                "'oneOnOne' (exactly 2 members). Only group chats support a topic. Members are "
                "specified as a list of Entra user IDs (the 'id' field returned by teams_search_users). "
                "The authenticated user is added automatically — do NOT include them again."
            ),
            parameters={
                "member_ids": ToolParameter(
                    description=(
                        "List of Microsoft Entra user IDs (GUIDs) to add as members. "
                        "Must NOT include the authenticated user's own ID — they are "
                        "added automatically as owner."
                    ),
                    type="array",
                    required=True,
                ),
                "chat_type": ToolParameter(
                    description="Either 'group' (3+ members incl. self) or 'oneOnOne' (1 other member).",
                    type="string",
                    required=True,
                ),
                "topic": ToolParameter(
                    description="Topic (display name) for the chat. Only valid when chat_type is 'group'.",
                    type="string",
                    required=False,
                ),
            },
        )

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        chat_type = params["chat_type"]
        if chat_type not in ("group", "oneOnOne"):
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=f"chat_type must be 'group' or 'oneOnOne', got '{chat_type}'",
                params=params,
            )

        member_ids: List[str] = params["member_ids"]
        if chat_type == "oneOnOne" and len(member_ids) != 1:
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error="oneOnOne chats require exactly 1 member id (the other participant).",
                params=params,
            )
        if chat_type == "group" and len(member_ids) < 2:
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error="group chats require at least 2 member ids in addition to the authenticated user.",
                params=params,
            )

        # Resolve the authenticated user's id so we can add ourselves explicitly.
        try:
            me, _ = self._toolset._graph_request("GET", "/me", timeout=10)
            my_id = me["id"]
        except Exception as e:
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=f"Failed to resolve authenticated user id for chat creation: {e}",
                params=params,
            )

        all_ids = [my_id] + [m for m in member_ids if m != my_id]
        members = [
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"{self._toolset.teams_config.graph_base_url.rstrip('/')}/users/{uid}",
            }
            for uid in all_ids
        ]

        body: Dict[str, Any] = {"chatType": chat_type, "members": members}
        if params.get("topic"):
            if chat_type != "group":
                return StructuredToolResult(
                    status=StructuredToolResultStatus.ERROR,
                    error="Topic can only be set on 'group' chats.",
                    params=params,
                )
            body["topic"] = params["topic"]

        return self._graph_request(
            "POST", "/chats", params_for_result=params, json_body=body
        )

    def get_parameterized_one_liner(self, params: Dict) -> str:
        topic = params.get("topic")
        label = f"'{topic}'" if topic else params.get("chat_type", "chat")
        return f"{toolset_name_for_one_liner(self._toolset.name)}: create chat {label}"


class TeamsSendChatMessage(BaseTeamsTool):
    def __init__(self, toolset: TeamsToolset):
        super().__init__(
            toolset=toolset,
            name="teams_send_chat_message",
            description=(
                "Post a plain-text message into an existing Microsoft Teams chat. "
                "Use chat_id returned by teams_create_chat or teams_list_chats."
            ),
            parameters={
                "chat_id": ToolParameter(
                    description="The chat ID (from teams_create_chat or teams_list_chats).",
                    type="string",
                    required=True,
                ),
                "content": ToolParameter(
                    description="Plain-text message body.",
                    type="string",
                    required=True,
                ),
            },
        )

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        return self._graph_request(
            "POST",
            f"/chats/{params['chat_id']}/messages",
            params_for_result=params,
            json_body={
                "body": {"contentType": "text", "content": params["content"]},
            },
        )

    def get_parameterized_one_liner(self, params: Dict) -> str:
        preview = (params.get("content") or "")[:40]
        return (
            f"{toolset_name_for_one_liner(self._toolset.name)}: "
            f"post to chat {params.get('chat_id', '')[:20]}… '{preview}'"
        )


class TeamsListChats(BaseTeamsTool):
    def __init__(self, toolset: TeamsToolset):
        super().__init__(
            toolset=toolset,
            name="teams_list_chats",
            description=(
                "List the authenticated user's Teams chats (1:1, group, and meeting chats). "
                "Supports optional topic_filter for exact match on the chat topic."
            ),
            parameters={
                "topic_filter": ToolParameter(
                    description="If provided, returns only chats whose topic equals this string.",
                    type="string",
                    required=False,
                ),
                "limit": ToolParameter(
                    description="Maximum number of chats to return (default 20, max 50).",
                    type="integer",
                    required=False,
                ),
            },
        )

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        query: Dict[str, Any] = {"$top": min(int(params.get("limit") or 20), 50)}
        if params.get("topic_filter"):
            safe = params["topic_filter"].replace("'", "''")
            query["$filter"] = f"topic eq '{safe}'"
        return self._graph_request(
            "GET", "/me/chats", params_for_result=params, query=query
        )

    def get_parameterized_one_liner(self, params: Dict) -> str:
        tf = params.get("topic_filter")
        suffix = f" topic='{tf}'" if tf else ""
        return f"{toolset_name_for_one_liner(self._toolset.name)}: list chats{suffix}"


class TeamsGetChatMessages(BaseTeamsTool):
    def __init__(self, toolset: TeamsToolset):
        super().__init__(
            toolset=toolset,
            name="teams_get_chat_messages",
            description=(
                "Retrieve recent messages from a Teams chat. Returns messages in reverse chronological order."
            ),
            parameters={
                "chat_id": ToolParameter(
                    description="The chat ID (from teams_list_chats or teams_create_chat).",
                    type="string",
                    required=True,
                ),
                "limit": ToolParameter(
                    description="Maximum number of messages to return (default 20, max 50).",
                    type="integer",
                    required=False,
                ),
            },
        )

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        query = {"$top": min(int(params.get("limit") or 20), 50)}
        return self._graph_request(
            "GET",
            f"/chats/{params['chat_id']}/messages",
            params_for_result=params,
            query=query,
        )

    def get_parameterized_one_liner(self, params: Dict) -> str:
        return (
            f"{toolset_name_for_one_liner(self._toolset.name)}: "
            f"read messages from chat {params.get('chat_id', '')[:20]}…"
        )

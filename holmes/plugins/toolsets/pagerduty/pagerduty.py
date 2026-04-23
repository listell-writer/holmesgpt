import logging
from typing import Any, ClassVar, Literal, Optional, Tuple, Type
from urllib.parse import urlparse

import requests  # type: ignore
from pydantic import Field

from holmes.core.tools import CallablePrerequisite, Toolset, ToolsetTag
from holmes.plugins.toolsets.http.http_toolset import (
    AuthConfig,
    EndpointConfig,
    HttpToolset,
    HttpToolsetConfig,
)
from holmes.utils.pydantic_utils import ToolsetConfig

logger = logging.getLogger(__name__)

PAGERDUTY_API_HOSTS = {
    "us": "api.pagerduty.com",
    "eu": "api.eu.pagerduty.com",
}

PAGERDUTY_ACCEPT_HEADER = "application/vnd.pagerduty+json;version=2"

# Read-only endpoint patterns. Globs (fnmatch) matched against request path.
READ_PATH_PATTERNS = [
    "/abilities",
    "/incidents",
    "/incidents/*",
    "/services",
    "/services/*",
    "/schedules",
    "/schedules/*",
    "/oncalls",
    "/escalation_policies",
    "/escalation_policies/*",
    "/users",
    "/users/*",
    "/teams",
    "/teams/*",
    "/log_entries",
    "/log_entries/*",
    "/change_events",
    "/change_events/*",
    "/event_orchestrations",
    "/event_orchestrations/*",
    "/incident_workflows",
    "/incident_workflows/*",
    "/status_pages",
    "/status_pages/*",
    "/response_plays",
    "/response_plays/*",
    "/business_services",
    "/business_services/*",
    "/maintenance_windows",
    "/maintenance_windows/*",
    "/priorities",
    "/addons",
    "/notifications",
    "/tags",
    "/tags/*",
    "/audit/records",
    "/audit/records/*",
]

# Additional paths permitted when enable_write=True. These are the endpoints
# investigation workflows most commonly need (add notes, request responders,
# manage incident status). Not a full PagerDuty write surface.
WRITE_PATH_PATTERNS = [
    "/incidents",
    "/incidents/*",
    "/incidents/*/notes",
    "/incidents/*/responder_requests",
    "/incidents/*/status_updates",
    "/incidents/*/snooze",
    "/incidents/*/merge",
]


class PagerDutyConfig(ToolsetConfig):
    """Configuration for PagerDuty REST API access.

    Example configuration:
    ```yaml
    api_key: "{{ env.PAGERDUTY_USER_API_KEY }}"
    region: "us"
    ```

    A User API Token is required. Generate one under:
      My Profile → User Settings → API Access → Create API User Token.
    """

    api_key: str = Field(
        title="API Key",
        description="PagerDuty User API Token.",
        examples=["u+abc123XYZ"],
    )
    region: Literal["us", "eu"] = Field(
        default="us",
        title="Region",
        description="PagerDuty region. Selects api.pagerduty.com (us) or api.eu.pagerduty.com (eu).",
    )
    enable_write: bool = Field(
        default=False,
        title="Enable Write",
        description=(
            "Allow POST/PUT to a curated set of incident-management endpoints "
            "(notes, responder requests, status changes). Disabled by default."
        ),
    )
    from_email: Optional[str] = Field(
        default=None,
        title="From Email",
        description=(
            "Email of the user whose token this is. Sent as the 'From' header, "
            "required by PagerDuty for POST /incidents and POST /incidents/{id}/notes."
        ),
    )


class PagerDutyToolset(Toolset):
    """PagerDuty toolset — thin wrapper that delegates to the generic HTTP toolset."""

    config_classes: ClassVar[list[Type[PagerDutyConfig]]] = [PagerDutyConfig]

    def __init__(self) -> None:
        super().__init__(
            name="pagerduty",
            description="Query PagerDuty incidents, services, schedules, on-call, escalation policies, and log entries.",
            icon_url="https://platform.robusta.dev/demos/pagerduty.svg",
            docs_url="https://holmesgpt.dev/data-sources/builtin-toolsets/pagerduty/",
            prerequisites=[CallablePrerequisite(callable=self.prerequisites_callable)],
            tools=[],
            tags=[ToolsetTag.CORE],
        )

    @property
    def _conf(self) -> PagerDutyConfig:
        return self.config  # type: ignore[return-value]

    def _api_host(self) -> str:
        return PAGERDUTY_API_HOSTS[self._conf.region]

    def _api_base(self) -> str:
        return f"https://{self._api_host()}"

    def prerequisites_callable(self, config: dict[str, Any]) -> Tuple[bool, str]:
        try:
            self.config = PagerDutyConfig(**config)
            ok, msg = self._perform_health_check()
            if not ok:
                return False, msg
            self._setup_http_tools()
            return True, msg
        except Exception as e:
            return False, f"Failed to validate PagerDuty configuration: {e}"

    def _perform_health_check(self) -> Tuple[bool, str]:
        url = f"{self._api_base()}/abilities"
        headers = {
            "Authorization": f"Token token={self._conf.api_key}",
            "Accept": PAGERDUTY_ACCEPT_HEADER,
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
        except requests.exceptions.ConnectionError as e:
            return False, f"Failed to connect to PagerDuty at {url}: {e}"
        except requests.exceptions.Timeout:
            return False, "PagerDuty health check timed out"
        except Exception as e:
            return False, f"PagerDuty health check failed: {e}"

        if response.status_code == 401:
            return (
                False,
                f"PagerDuty authentication failed (HTTP 401). Check PAGERDUTY_USER_API_KEY. Body: {response.text[:200]}",
            )
        if response.status_code == 403:
            return (
                False,
                f"PagerDuty access denied (HTTP 403). The token may lack required permissions. Body: {response.text[:200]}",
            )
        if not response.ok:
            return (
                False,
                f"PagerDuty API error: HTTP {response.status_code} {response.text[:200]}",
            )
        return True, f"PagerDuty API is accessible ({self._api_host()})."

    def _build_endpoint_config(self) -> EndpointConfig:
        methods = ["GET"]
        paths = list(READ_PATH_PATTERNS)
        if self._conf.enable_write:
            methods.extend(["POST", "PUT"])
            # De-duplicate while preserving order.
            seen = set(paths)
            for p in WRITE_PATH_PATTERNS:
                if p not in seen:
                    paths.append(p)
                    seen.add(p)

        return EndpointConfig(
            hosts=[self._api_host()],
            paths=paths,
            methods=methods,
            auth=AuthConfig(
                type="header",
                name="Authorization",
                value=f"Token token={self._conf.api_key}",
            ),
            health_check_url=f"{self._api_base()}/abilities",
        )

    def _build_default_headers(self) -> dict[str, str]:
        # PagerDuty requires the versioned Accept header for v2-shape responses.
        headers = {"Accept": PAGERDUTY_ACCEPT_HEADER}
        if self._conf.from_email:
            # Required by POST /incidents and POST /incidents/{id}/notes.
            # Harmless on GETs.
            headers["From"] = self._conf.from_email
        return headers

    def _build_llm_instructions(self) -> str:
        base = self._api_base()
        write_note = (
            "\n\n**Write operations are enabled.** Allowed mutations: POST to "
            "/incidents, /incidents/{id}/notes, /incidents/{id}/responder_requests, "
            "/incidents/{id}/status_updates, /incidents/{id}/snooze, /incidents/{id}/merge; "
            "PUT to /incidents. All other mutating endpoints are blocked."
            if self._conf.enable_write
            else "\n\nWrite operations are disabled. Only GET requests are allowed."
        )
        return f"""### PagerDuty REST API

Base URL: {base}
Auth: `Authorization: Token token=<user-api-token>` (set by the toolset; do not include in your request).
Accept: `application/vnd.pagerduty+json;version=2` (set by the toolset).

**Common read endpoints:**

- `GET /incidents` — list incidents. Useful query params:
  `since`, `until` (ISO 8601), `statuses[]=triggered|acknowledged|resolved`,
  `service_ids[]=P...`, `user_ids[]=P...`, `team_ids[]=P...`,
  `urgencies[]=high|low`, `incident_key=<key>`, `limit`, `offset`, `total=true`,
  `include[]=assignees|services|first_trigger_log_entries|teams|priorities`.
- `GET /incidents/{{id}}` — one incident; add `include[]=...` for sideloading.
- `GET /incidents/{{id}}/notes` — notes added to an incident.
- `GET /incidents/{{id}}/log_entries` — full activity timeline.
- `GET /incidents/{{id}}/alerts` — alerts grouped under the incident.
- `GET /services`, `GET /services/{{id}}` — services.
- `GET /schedules`, `GET /schedules/{{id}}` — on-call schedules.
- `GET /oncalls` — current on-call users. Params: `schedule_ids[]`, `user_ids[]`, `since`, `until`, `earliest=true`.
- `GET /escalation_policies`, `GET /escalation_policies/{{id}}` — escalation policies.
- `GET /users`, `GET /users/{{id}}` — users.
- `GET /teams`, `GET /teams/{{id}}` — teams.
- `GET /log_entries` — account-wide activity log.
- `GET /change_events` — deploy/config change events.
- `GET /abilities` — lists features this account has purchased (e.g. `teams`,
  `custom_fields_on_incidents`, `event_intelligence`, `incident_workflows`).
  Use this when an endpoint returns 402/403 to determine whether a plan add-on is required.

**Pagination:** default `limit` is 25, max 100 on most endpoints. Paginate with
`offset` until `more=false` in the response. For large queries, add `total=true`
to get `total` in the response.

**Plan-gated endpoints (will 402/403 on accounts without the add-on):**

- `/incidents/{{id}}/outlier_incident`, `/past_incidents`, `/related_incidents` — require AIOps / Event Intelligence.
- `/incident_workflows`, `/incidents/{{id}}/custom_fields` — Business plan and above.
- `/audit/records` — Enterprise plan.
- `/analytics/*` — not allowed via this toolset; use PagerDuty's Analytics API directly if needed.

**Incident keys:** `incident_key` is a dedup key. To find an incident you seeded
with a known key, use `GET /incidents?incident_key=<key>` (plus status filters
if the incident may already be resolved).
{write_note}
"""

    def _setup_http_tools(self) -> None:
        endpoint = self._build_endpoint_config()
        http_config = HttpToolsetConfig(
            endpoints=[endpoint],
            default_headers=self._build_default_headers(),
        )
        http_toolset = HttpToolset(
            name="pagerduty",
            config=http_config,
            llm_instructions=self._build_llm_instructions(),
            enabled=True,
        )
        ok, msg = http_toolset.prerequisites_callable(http_config.model_dump())
        if not ok:
            raise RuntimeError(f"Failed to initialize HTTP toolset for PagerDuty: {msg}")

        self.tools = http_toolset.tools
        self.llm_instructions = http_toolset.llm_instructions

        # Sanity check: the endpoint host must be one of our known hosts.
        parsed = urlparse(self._api_base())
        if parsed.hostname not in PAGERDUTY_API_HOSTS.values():
            raise RuntimeError(
                f"Unexpected PagerDuty host {parsed.hostname}; expected one of {list(PAGERDUTY_API_HOSTS.values())}"
            )

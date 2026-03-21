"""
File-based token storage for MCP OAuth tokens.

Implements the TokenStorage protocol from the MCP SDK, storing
OAuth tokens and client registration info in ~/.holmes/tokens/.
Each MCP server gets its own file, keyed by a hash of the server URL.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

DEFAULT_TOKENS_DIR = Path.home() / ".holmes" / "tokens"


class FileTokenStorage:
    """Stores OAuth tokens and client info on disk.

    Implements the MCP SDK's TokenStorage protocol:
    - get_tokens / set_tokens: Access/refresh token persistence
    - get_client_info / set_client_info: Dynamic client registration persistence
    """

    def __init__(self, server_url: str, tokens_dir: Optional[Path] = None) -> None:
        self._tokens_dir = tokens_dir or DEFAULT_TOKENS_DIR
        self._server_hash = hashlib.sha256(server_url.encode()).hexdigest()[:16]
        self._tokens_path = self._tokens_dir / f"{self._server_hash}_tokens.json"
        self._client_info_path = self._tokens_dir / f"{self._server_hash}_client.json"

    def _ensure_dir(self) -> None:
        self._tokens_dir.mkdir(parents=True, exist_ok=True)
        # Restrict permissions to owner only
        self._tokens_dir.chmod(0o700)

    async def get_tokens(self) -> Optional[OAuthToken]:
        if not self._tokens_path.exists():
            return None
        try:
            data = json.loads(self._tokens_path.read_text())
            return OAuthToken.model_validate(data)
        except Exception:
            logger.warning(f"Failed to load tokens from {self._tokens_path}, ignoring cached tokens")
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._ensure_dir()
        self._tokens_path.write_text(tokens.model_dump_json(indent=2))
        self._tokens_path.chmod(0o600)

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        if not self._client_info_path.exists():
            return None
        try:
            data = json.loads(self._client_info_path.read_text())
            return OAuthClientInformationFull.model_validate(data)
        except Exception:
            logger.warning(f"Failed to load client info from {self._client_info_path}, ignoring")
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._ensure_dir()
        self._client_info_path.write_text(client_info.model_dump_json(indent=2))
        self._client_info_path.chmod(0o600)

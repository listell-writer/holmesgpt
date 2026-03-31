"""Tests for ServiceNow toolset configuration"""

import pytest

from holmes.plugins.toolsets.servicenow.servicenow import (
    ServiceNowConfig,
)


class TestServiceNowConfig:
    """Tests for servicenow configuration validation"""

    def test_config_api_key_valid(self):
        config = ServiceNowConfig(api_url="https://example.service-now.com", api_key="now_abc")
        assert config.api_key == "now_abc"
        assert config.username is None
        assert config.password is None

    def test_config_basic_auth_valid(self):
        config = ServiceNowConfig(
            api_url="https://example.service-now.com",
            username="api-user",
            password="secret-pass",
        )
        assert config.username == "api-user"
        assert config.password == "secret-pass"
        assert config.api_key is None

    def test_config_no_auth_valid(self):
        config = ServiceNowConfig(api_url="https://example.service-now.com")
        assert config.username is None
        assert config.password is None
        assert config.api_key is None

    def test_config_both_authentication_methods_fails(self):
        with pytest.raises(ValueError, match="authentication method must be either api key or basic auth, not both"):
            ServiceNowConfig(
                api_url="https://example.service-now.com",
                api_key="now_abc",
                username="api-user",
                password="secret-pass",
            )

    def test_config_username_without_password_fails(self):
        with pytest.raises(ValueError, match="password is required when username is set"):
            ServiceNowConfig(
                api_url="https://example.service-now.com",
                username="api-user",
            )

    def test_config_password_without_username_fails(self):
        with pytest.raises(ValueError, match="username is required when password is set"):
            ServiceNowConfig(
                api_url="https://example.service-now.com",
                password="secret-pass",
            )

import os

import pytest

from holmes.utils.env import get_env_replacement, replace_env_vars_values


@pytest.mark.parametrize(
    "input_value, mock_environ, expected_output",
    [
        ("this is a plain string", {}, "this is a plain string"),
        ("{{ other_format.VAR }}", {}, "{{ other_format.VAR }}"),
        ("{{env.VAR}", {}, "{{env.VAR}"),
        ("{ env.VAR }}", {}, "{ env.VAR }}"),
        ("{{ foo.bar }}", {}, "{{ foo.bar }}"),
        ("{{ VAR }}", {}, "{{ VAR }}"),
        ("{{ env.MY_VAR }}", {"MY_VAR": "var_value_123"}, "var_value_123"),
        (
            "{{  env.MY_VAR_SPACED  }}",
            {"MY_VAR_SPACED": "spaced_value_456"},
            "spaced_value_456",
        ),
        (
            "prefix {{ env.MY_VAR }} suffix",
            {"MY_VAR": "var_value_789"},
            "prefix var_value_789 suffix",
        ),
        (
            "{{ env.FIRST_VAR }} {{ env.SECOND_VAR }}",
            {"FIRST_VAR": "first_val", "SECOND_VAR": "second_val"},
            "first_val second_val",
        ),
        ("{{ env.EMPTY_VAL_VAR }}", {"EMPTY_VAL_VAR": ""}, ""),
        (
            "{{ env.app.config.host }}",
            {"app.config.host": "localhost.localdomain"},
            "localhost.localdomain",
        ),
        (
            "foo_{{ env.MYKEY }}_bar",
            {"MYKEY": "my_value_for_mykey"},
            "foo_my_value_for_mykey_bar",
        ),
        (
            "this is a {{ env.MYKEY }} env var",
            {"MYKEY": "special"},
            "this is a special env var",
        ),
    ],
)
def test_get_env_replacement_successful(
    input_value, mock_environ, expected_output, monkeypatch
):
    """
    Tests various scenarios where get_env_replacement should return a value (or None)
    without raising an exception.
    """
    # monkeypatch.setattr(os, 'environ', mock_environ) # This replaces the whole dict
    # A better way for os.environ is to set/unset specific keys if needed, or use clear and update
    monkeypatch.setattr(os, "environ", mock_environ.copy())  # Ensure we use a copy

    actual_output = get_env_replacement(input_value)
    assert actual_output == expected_output


@pytest.mark.parametrize(
    "input_value, mock_environ, expected_exception_type, expected_exception_message_regex",
    [
        (
            "{{ env.NON_EXISTENT_VAR }}",
            {},  # Ensure the variable is not in the environment
            Exception,
            r"ENV var replacement NON_EXISTENT_VAR does not exist",
        ),
        (
            "{{ env. }}",
            {},
            Exception,
            r"ENV var replacement  does not exist",  # Note: two spaces for empty key
        ),
    ],
)
def test_get_env_replacement_exceptions(
    input_value,
    mock_environ,
    expected_exception_type,
    expected_exception_message_regex,
    monkeypatch,
):
    """
    Tests scenarios where get_env_replacement is expected to raise an Exception
    and log an error.
    """
    monkeypatch.setattr(os, "environ", mock_environ.copy())

    with pytest.raises(expected_exception_type, match=expected_exception_message_regex):
        get_env_replacement(input_value)


class TestReplaceEnvVarsValues:
    """Tests for replace_env_vars_values with nested structures (dicts and lists)."""

    def test_flat_dict(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "secret123")
        monkeypatch.setenv("API_URL", "https://example.com")
        values = {
            "api_key": "{{ env.API_KEY }}",
            "api_url": "{{ env.API_URL }}",
            "static_field": "no_replacement",
        }
        result = replace_env_vars_values(values)
        assert result["api_key"] == "secret123"
        assert result["api_url"] == "https://example.com"
        assert result["static_field"] == "no_replacement"

    def test_nested_dict(self, monkeypatch):
        monkeypatch.setenv("DB_HOST", "db.example.com")
        values = {
            "config": {
                "host": "{{ env.DB_HOST }}",
                "port": 5432,
            }
        }
        result = replace_env_vars_values(values)
        assert result["config"]["host"] == "db.example.com"
        assert result["config"]["port"] == 5432

    def test_list_of_strings(self, monkeypatch):
        monkeypatch.setenv("HOST1", "h1.example.com")
        monkeypatch.setenv("HOST2", "h2.example.com")
        values = {
            "hosts": ["{{ env.HOST1 }}", "{{ env.HOST2 }}", "static"]
        }
        result = replace_env_vars_values(values)
        assert result["hosts"] == ["h1.example.com", "h2.example.com", "static"]

    def test_list_of_dicts_with_env_vars(self, monkeypatch):
        """Reproducer for the reported bug: env vars inside list items (dicts) not resolved.

        This mimics the RabbitMQ/Kafka clusters config structure:
        clusters:
          - name: DEV
            username: "{{ env.RABBITMQ_USERNAME }}"
            password: "{{ env.RABBITMQ_PASSWORD }}"
        """
        monkeypatch.setenv("RABBITMQ_USERNAME", "my_rabbit_user")
        monkeypatch.setenv("RABBITMQ_PASSWORD", "my_rabbit_pass")
        values = {
            "config": {
                "clusters": [
                    {
                        "id": "my-rabbit",
                        "api_url": "http://rabbitmq.local:15672",
                        "username": "{{ env.RABBITMQ_USERNAME }}",
                        "password": "{{ env.RABBITMQ_PASSWORD }}",
                    }
                ]
            }
        }
        result = replace_env_vars_values(values)
        cluster = result["config"]["clusters"][0]
        assert cluster["username"] == "my_rabbit_user"
        assert cluster["password"] == "my_rabbit_pass"
        assert cluster["api_url"] == "http://rabbitmq.local:15672"
        assert cluster["id"] == "my-rabbit"

    def test_multiple_clusters_with_env_vars(self, monkeypatch):
        """Mimics the Kafka multi-cluster config from the bug report."""
        monkeypatch.setenv("KAFKA_DEV_PLATFORM_API_KEY", "dev_key")
        monkeypatch.setenv("KAFKA_DEV_PLATFORM_SECRET", "dev_secret")
        monkeypatch.setenv("KAFKA_DEV_ANALYTICS_API_KEY", "analytics_key")
        monkeypatch.setenv("KAFKA_DEV_ANALYTICS_SECRET", "analytics_secret")
        values = {
            "enabled": True,
            "config": {
                "clusters": [
                    {
                        "name": "DEV_PLATFORM",
                        "broker": "pkc-4nym6.us-east-1.aws.confluent.cloud:9092",
                        "username": "{{ env.KAFKA_DEV_PLATFORM_API_KEY }}",
                        "password": "{{ env.KAFKA_DEV_PLATFORM_SECRET }}",
                        "sasl_mechanism": "PLAIN",
                        "security_protocol": "SASL_SSL",
                    },
                    {
                        "name": "DEV_ANALYTICS",
                        "broker": "pkc-d93do.us-east-1.aws.confluent.cloud:9092",
                        "username": "{{ env.KAFKA_DEV_ANALYTICS_API_KEY }}",
                        "password": "{{ env.KAFKA_DEV_ANALYTICS_SECRET }}",
                        "sasl_mechanism": "PLAIN",
                        "security_protocol": "SASL_SSL",
                    },
                ],
            },
        }
        result = replace_env_vars_values(values)
        cluster1 = result["config"]["clusters"][0]
        cluster2 = result["config"]["clusters"][1]
        assert cluster1["username"] == "dev_key"
        assert cluster1["password"] == "dev_secret"
        assert cluster2["username"] == "analytics_key"
        assert cluster2["password"] == "analytics_secret"

    def test_empty_env_var_value_in_nested_list(self, monkeypatch):
        """An env var set to empty string should still replace the placeholder."""
        monkeypatch.setenv("EMPTY_USER", "")
        values = {
            "clusters": [
                {
                    "username": "{{ env.EMPTY_USER }}",
                }
            ]
        }
        result = replace_env_vars_values(values)
        assert result["clusters"][0]["username"] == ""

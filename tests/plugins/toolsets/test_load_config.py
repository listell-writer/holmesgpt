import os

import pytest
import yaml

from holmes.plugins.toolsets import load_toolsets_from_config


def test_load_toolsets_from_config_old_format():
    old_format_data = [
        {
            "name": "aws/security",
            "prerequisites": [{"command": "aws sts get-caller-identity"}],
            "tools": [
                {
                    "name": "aws_cloudtrail_event_lookup",
                    "description": "Fetches events from AWS CloudTrail",
                    "command": "aws cloudtrail lookup-events",
                }
            ],
        }
    ]

    with pytest.raises(ValueError, match="Old toolset config format detected"):
        load_toolsets_from_config(old_format_data)


def test_load_toolsets_from_config_multiple_old_format_toolsets():
    old_format_data = [
        {
            "name": "aws/security",
            "prerequisites": [{"command": "aws sts get-caller-identity"}],
            "tools": [
                {
                    "name": "aws_cloudtrail_event_lookup",
                    "description": "Fetches events from AWS CloudTrail",
                    "command": "aws cloudtrail lookup-events",
                }
            ],
        },
        {
            "name": "kubernetes/logs",
            "tools": [
                {
                    "name": "kubectl_logs",
                    "description": "Fetch Kubernetes logs",
                    "command": "kubectl logs",
                }
            ],
        },
    ]

    with pytest.raises(ValueError, match="Old toolset config format detected"):
        load_toolsets_from_config(old_format_data)


toolsets_config_str = """
grafana/loki:
    config:
        api_key: "{{env.GRAFANA_API_KEY}}"
        api_url: "{{env.GRAFANA_URL}}"
        grafana_datasource_uid: "my_grafana_datasource_uid"
"""

env_vars = {
    "GRAFANA_API_KEY": "glsa_sdj1q2o3prujpqfd",
    "GRAFANA_URL": "https://my-grafana.com/",
}


def test_load_toolsets_from_config(monkeypatch):
    for key, value in env_vars.items():
        os.environ[key] = value
        monkeypatch.setenv(key, value)

    toolsets_config = yaml.safe_load(toolsets_config_str)
    assert isinstance(toolsets_config, dict)
    definitions = load_toolsets_from_config(
        toolsets=toolsets_config, strict_check=False
    )
    assert len(definitions) == 1
    grafana_loki = definitions[0]
    config = grafana_loki.config
    assert config
    assert config.get("api_key") == "glsa_sdj1q2o3prujpqfd"
    assert config.get("api_url") == "https://my-grafana.com/"
    assert config.get("grafana_datasource_uid") == "my_grafana_datasource_uid"


# Config with env vars nested inside a list (clusters pattern)
toolsets_config_nested_str = """
rabbitmq/core:
    enabled: true
    config:
        clusters:
            - id: my-rabbit
              api_url: "http://rabbitmq.local:15672"
              username: "{{ env.RABBITMQ_USERNAME }}"
              password: "{{ env.RABBITMQ_PASSWORD }}"
"""

nested_env_vars = {
    "RABBITMQ_USERNAME": "rabbit_user_123",
    "RABBITMQ_PASSWORD": "rabbit_pass_456",
}


def test_load_toolsets_from_config_nested_env_vars(monkeypatch):
    """Env vars inside list items (e.g. clusters) must be resolved."""
    for key, value in nested_env_vars.items():
        monkeypatch.setenv(key, value)

    toolsets_config = yaml.safe_load(toolsets_config_nested_str)
    definitions = load_toolsets_from_config(
        toolsets=toolsets_config, strict_check=False
    )
    assert len(definitions) == 1
    rabbitmq = definitions[0]
    config = rabbitmq.config
    assert config
    clusters = config.get("clusters")
    assert clusters and len(clusters) == 1
    cluster = clusters[0]
    assert cluster["username"] == "rabbit_user_123"
    assert cluster["password"] == "rabbit_pass_456"
    assert cluster["api_url"] == "http://rabbitmq.local:15672"


# Config mimicking Kafka multi-cluster with env vars in list items
toolsets_config_kafka_str = """
kafka/admin:
    enabled: true
    config:
        clusters:
            - name: DEV_PLATFORM
              broker: "pkc-4nym6.us-east-1.aws.confluent.cloud:9092"
              username: "{{ env.KAFKA_DEV_PLATFORM_API_KEY }}"
              password: "{{ env.KAFKA_DEV_PLATFORM_SECRET }}"
              sasl_mechanism: PLAIN
              security_protocol: SASL_SSL
            - name: DEV_ANALYTICS
              broker: "pkc-d93do.us-east-1.aws.confluent.cloud:9092"
              username: "{{ env.KAFKA_DEV_ANALYTICS_API_KEY }}"
              password: "{{ env.KAFKA_DEV_ANALYTICS_SECRET }}"
              sasl_mechanism: PLAIN
              security_protocol: SASL_SSL
"""

kafka_env_vars = {
    "KAFKA_DEV_PLATFORM_API_KEY": "platform_key",
    "KAFKA_DEV_PLATFORM_SECRET": "platform_secret",
    "KAFKA_DEV_ANALYTICS_API_KEY": "analytics_key",
    "KAFKA_DEV_ANALYTICS_SECRET": "analytics_secret",
}


def test_load_toolsets_from_config_kafka_multi_cluster_env_vars(monkeypatch):
    """Env vars in multiple list items must all be resolved (Kafka multi-cluster)."""
    for key, value in kafka_env_vars.items():
        monkeypatch.setenv(key, value)

    toolsets_config = yaml.safe_load(toolsets_config_kafka_str)
    definitions = load_toolsets_from_config(
        toolsets=toolsets_config, strict_check=False
    )
    assert len(definitions) == 1
    kafka = definitions[0]
    config = kafka.config
    assert config
    clusters = config.get("clusters")
    assert clusters and len(clusters) == 2

    assert clusters[0]["username"] == "platform_key"
    assert clusters[0]["password"] == "platform_secret"
    assert clusters[1]["username"] == "analytics_key"
    assert clusters[1]["password"] == "analytics_secret"

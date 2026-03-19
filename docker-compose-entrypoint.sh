#!/bin/sh
# Docker Compose entrypoint for HolmesGPT
# Handles kubeconfig setup and optional AWS CLI installation for EKS auth

set -e

# Copy kubeconfig (mounted read-only) so we can modify it
mkdir -p /root/.kube
if [ -f /tmp/.kube/config ]; then
    cp /tmp/.kube/config /root/.kube/config

    # Rewrite localhost API server addresses to host.docker.internal
    sed -i 's|server: https://127\.0\.0\.1|server: https://host.docker.internal|g; s|server: https://localhost|server: https://host.docker.internal|g' /root/.kube/config

    # Install AWS CLI if kubeconfig uses EKS exec-based auth and aws is not already installed
    if grep -q 'command: aws' /root/.kube/config 2>/dev/null && ! command -v aws >/dev/null 2>&1; then
        echo "EKS exec-based auth detected in kubeconfig, installing AWS CLI..."
        ARCH=$(dpkg --print-architecture)
        if [ "$ARCH" = "amd64" ]; then AWS_ARCH="x86_64"; else AWS_ARCH="aarch64"; fi
        curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscliv2.zip
        unzip -q /tmp/awscliv2.zip -d /tmp
        /tmp/aws/install
        rm -rf /tmp/awscliv2.zip /tmp/aws
        echo "AWS CLI installed: $(aws --version)"
    fi
fi

exec python -u server.py

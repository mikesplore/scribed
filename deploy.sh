#!/bin/bash
set -euo pipefail

CONTAINER_NAME="scribed"
IMAGE_NAME="mikesplore/scribed:latest"
NETWORK_NAME="scribed-network"
PORT="9005"

if ! docker network ls --format '{{.Name}}' | grep -q "^${NETWORK_NAME}$"; then
  echo "🌐 Creating network ${NETWORK_NAME}..."
  docker network create "${NETWORK_NAME}"
else
  echo "🌐 Network ${NETWORK_NAME} already exists."
fi

echo "🚀 Pulling latest image..."
docker pull "${IMAGE_NAME}"

echo "🛑 Stopping and removing old container..."
docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true

echo "⚡ Starting new container on port ${PORT}..."
docker run -d \
  --name "${CONTAINER_NAME}" \
  --network "${NETWORK_NAME}" \
  --restart unless-stopped \
  --env-file .env \
  -e "PORT=${PORT}" \
  -p "${PORT}:${PORT}" \
  "${IMAGE_NAME}"

echo "✅ Verifying container..."
docker exec "${CONTAINER_NAME}" printenv | grep -E '^(PORT|DATABASE_URL|POSTGRES)' || true

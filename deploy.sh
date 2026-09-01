#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONTAINER_NAME="scribed"
VERSION="${1:?Usage: $0 VERSION (for example 12)}"
IMAGE_NAME="mikesplore/scribed:${VERSION}"
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
  --env-file "${SCRIPT_DIR}/.env" \
  -e "PORT=${PORT}" \
  -p "${PORT}:${PORT}" \
  "${IMAGE_NAME}"

echo "✅ Verifying container health..."
for attempt in $(seq 1 30); do
  if curl --fail --silent "http://127.0.0.1:${PORT}/health" >/dev/null; then
    echo "Health check passed."; exit 0
  fi
  sleep 2
done
echo "Health check failed; container logs:"
docker logs "${CONTAINER_NAME}"
exit 1

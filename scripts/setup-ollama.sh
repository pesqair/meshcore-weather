#!/bin/bash
# Pull the required model into Ollama
# Run this after `docker compose up -d ollama` on first setup

OLLAMA_HOST="${1:-http://localhost:11434}"
MODEL="${2:-mistral}"

echo "Waiting for Ollama to be ready..."
until curl -s "$OLLAMA_HOST/api/tags" > /dev/null 2>&1; do
    sleep 1
done

echo "Pulling model: $MODEL"
curl -s "$OLLAMA_HOST/api/pull" -d "{\"name\": \"$MODEL\"}" | while read -r line; do
    status=$(echo "$line" | grep -o '"status":"[^"]*"' | head -1)
    echo "  $status"
done

echo "Done. Model $MODEL is ready."

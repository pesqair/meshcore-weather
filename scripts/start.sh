#!/bin/bash
# Start the meshcore-weather stack on macOS (OrbStack or Docker Desktop)
#
# Usage: ./scripts/start.sh [serial_port]
# Example: ./scripts/start.sh /dev/cu.usbserial-0001

set -e
SERIAL_PORT="${1:-/dev/cu.usbserial-0001}"
SOCAT_PORT=4403

# Check serial port exists
if [ ! -e "$SERIAL_PORT" ]; then
    echo "Serial port $SERIAL_PORT not found."
    echo "Available ports:"
    ls /dev/cu.usb* /dev/cu.serial* 2>/dev/null || echo "  (none)"
    exit 1
fi

# Start socat to bridge serial -> TCP (background)
echo "Bridging $SERIAL_PORT -> TCP port $SOCAT_PORT"
socat TCP-LISTEN:$SOCAT_PORT,reuseaddr,fork \
    OPEN:$SERIAL_PORT,raw,echo=0,ispeed=115200,ospeed=115200 &
SOCAT_PID=$!
echo "socat PID: $SOCAT_PID"

# Cleanup on exit
trap "echo 'Stopping socat...'; kill $SOCAT_PID 2>/dev/null; docker compose down" EXIT

# Start containers
echo "Starting containers..."
docker compose up -d

# Check host Ollama has the model
if command -v ollama &>/dev/null; then
    if ! ollama list 2>/dev/null | grep -q mistral; then
        echo "Pulling mistral model (first time, ~4GB)..."
        ollama pull mistral
    fi
fi

echo ""
echo "=== Meshcore Weather Bot running ==="
echo "  Serial: $SERIAL_PORT -> TCP :$SOCAT_PORT"
echo "  Logs:   docker compose logs -f meshcore-weather"
echo "  Stop:   Ctrl+C"
echo ""

# Follow logs
docker compose logs -f meshcore-weather

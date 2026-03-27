#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

VENV_DIR=".venv"
MARKER="$VENV_DIR/.deps_installed"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# Install deps only if marker is missing or requirements.txt changed
if [ ! -f "$MARKER" ] || [ requirements.txt -nt "$MARKER" ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
    touch "$MARKER"
else
    echo "Dependencies up to date, skipping install."
fi

echo "Starting server..."
python main.py

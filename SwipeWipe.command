#!/bin/bash
# Double-click on macOS to start SwipeWipe.
cd "$(dirname "$0")"
if ! python3 -c "import osxphotos" 2>/dev/null; then
  echo "Installing osxphotos (one-time)..."
  python3 -m pip install --user osxphotos pillow || { echo "pip install failed"; read -n1; exit 1; }
fi
python3 swipewipe.py

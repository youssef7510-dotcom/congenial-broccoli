#!/bin/sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"

PAGE=${1:-/}
case "$PAGE" in
  /|/admin) ;;
  *)
    printf 'Unknown page: %s\n' "$PAGE" >&2
    exit 2
    ;;
esac

PORT=${PORT:-5001}
URL="http://127.0.0.1:${PORT}${PAGE}"

if curl --fail --silent --output /dev/null "http://127.0.0.1:${PORT}/"; then
  printf 'The attendance app is already running. Opening %s\n' "$URL"
  open "$URL"
  exit 0
fi

PYTHON="$APP_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  printf 'Creating the application environment...\n'
  python3 -m venv "$APP_DIR/.venv"
fi

if ! "$PYTHON" -c 'import flask, cv2, numpy, openpyxl' >/dev/null 2>&1; then
  printf 'Installing application dependencies...\n'
  "$PYTHON" -m pip install --upgrade pip
  "$PYTHON" -m pip install -r "$APP_DIR/requirements.txt"
fi

printf '\nStarting AttendAI at http://127.0.0.1:%s\n' "$PORT"
if [ "$PAGE" = "/admin" ]; then
  printf 'Opening administrator sign-in. Demo login: admin / admin123\n'
else
  printf 'Opening the attendance station. Pair this browser in Admin first.\n'
fi
printf 'Keep this Terminal window open. Press Control-C to stop the server.\n\n'

"$PYTHON" "$APP_DIR/app.py" &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

attempt=0
until curl --fail --silent --output /dev/null "http://127.0.0.1:${PORT}/"; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    printf 'The attendance server stopped before it became ready. Check the error above.\n' >&2
    wait "$SERVER_PID"
    exit 1
  fi
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    printf 'The attendance server did not become ready in time.\n' >&2
    exit 1
  fi
  sleep 1
done

open "$URL"
wait "$SERVER_PID"

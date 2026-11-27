#!/bin/sh

# Exit immediately if a command exits with a non-zero status
set -e

# Set defaults for PUID, PGID, and paths if not provided
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
# The same variable and default as the app's DATA_DIR setting.
DATA_PATH="${DATA_DIR:-/data}"

# Define your application binary name in one place
APP_BIN="zabbcomm"

# If run as root, fix permissions and drop privileges
if [ "$(id -u)" = "0" ]; then
    # Ensure config and cache directories exist and match PUID/PGID.
    # Everything inside them too: after PUID/PGID change, or after a command was
    # run as root (e.g. `docker compose exec`), files can belong to someone else.
    # Only files with the wrong owner are touched, so this stays fast.
    for dir in "$DATA_PATH"; do
        mkdir -p "$dir"
        find "$dir" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown -h "$PUID:$PGID" {} +
    done

    # Drop privileges and execute the application
    exec setpriv --reuid "$PUID" --regid "$PGID" --clear-groups "$APP_BIN" "$@"
else
  # If not running as root, just ensure the directories exist
  for dir in "$DATA_PATH"; do
        mkdir -p "$dir"
  done

  # Execute the application under the existing container user
  exec "$APP_BIN" "$@"
fi
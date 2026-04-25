#!/usr/bin/env bash
# Usage: remember.sh <project> <content> [tags...]
# Example: remember.sh my-app "Use port 8443 for the API gateway" k8s architecture

set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: remember.sh <project> <content> [tags...]"
    echo "Example: remember.sh my-app \"Use port 8443 for the API\" k8s architecture"
    exit 1
fi

PROJECT="$1"
CONTENT="$2"
shift 2

TAG_JSON="\"project:${PROJECT}\""
for tag in "$@"; do
    TAG_JSON="${TAG_JSON},\"${tag}\""
done

curl -s -X POST http://localhost:8000/memories \
  -H "Content-Type: application/json" \
  -d "{\"content\":\"${CONTENT}\",\"source\":\"manual\",\"tags\":[${TAG_JSON}]}" | python3 -m json.tool

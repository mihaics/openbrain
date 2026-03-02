#!/bin/bash
set -e

echo "Starting Open Brain..."

# Load environment
export $(cat .env | grep -v '^#' | xargs) 2>/dev/null || true

# Set defaults
export DB_HOST=${DB_HOST:-postgres}
export DB_PORT=${DB_PORT:-5432}
export DB_NAME=${DB_NAME:-openbrain}
export DB_USER=${DB_USER:-postgres}
export DB_PASSWORD=${DB_PASSWORD:-openbrain}

# Check if database needs setup
echo "Checking database..."
python scripts/check_db.py

if [ $? -eq 1 ]; then
    echo "Database needs setup. Running setup..."
    python scripts/setup_db.py
else
    echo "Database already configured."
fi

# Start the application
echo "Starting application..."
exec "$@"

#!/bin/bash
# Open Brain Health Check Script
# Monitors the health of Open Brain services

set -e

# Configuration
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-openbrain}"
DB_USER="${DB_USER:-postgres}"
API_HOST="${API_HOST:-localhost}"
API_PORT="${API_PORT:-8000}"
OLLAMA_HOST="${OLLAMA_HOST:-localhost}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Track status
OVERALL_STATUS=0

check_service() {
    local name=$1
    local check_func=$2
    
    echo -n "Checking $name... "
    
    if $check_func; then
        echo -e "${GREEN}OK${NC}"
        return 0
    else
        echo -e "${RED}FAILED${NC}"
        OVERALL_STATUS=1
        return 1
    fi
}

# Database check
check_db() {
    if command -v pg_isready &> /dev/null; then
        PGPASSWORD="$DB_PASSWORD" pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" > /dev/null 2>&1
    else
        # Fallback: try to connect with psql
        echo "SELECT 1" | PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" > /dev/null 2>&1
    fi
}

# API check
check_api() {
    curl -s -f "http://$API_HOST:$API_PORT/health" > /dev/null 2>&1
}

# Ollama check
check_ollama() {
    curl -s -f "http://$OLLAMA_HOST:$OLLAMA_PORT/api/tags" > /dev/null 2>&1
}

# Disk space check
check_disk() {
    local usage=$(df -h . | tail -1 | awk '{print $5}' | sed 's/%//')
    [ "$usage" -lt 90 ]
}

# Memory check
check_memory() {
    local usage=$(free | grep Mem | awk '{print $3/$2 * 100}')
    [ "$usage" -lt 90 ]
}

# Database stats
get_db_stats() {
    if command -v psql &> /dev/null; then
        echo "Database stats:"
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" -t -c "SELECT COUNT(*) FROM memories;" 2>/dev/null || echo "  Unable to query"
    fi
}

# Main
echo "========================================="
echo "Open Brain Health Check"
echo "========================================="
echo ""

check_service "Database" check_db
check_service "API Server" check_api
check_service "Ollama (Embeddings)" check_ollama
check_service "Disk Space" check_disk
check_service "Memory" check_memory

echo ""
echo "========================================="
echo "Additional Info"
echo "========================================="

# Database memory count
get_db_stats

echo ""
if [ $OVERALL_STATUS -eq 0 ]; then
    echo -e "${GREEN}All checks passed!${NC}"
else
    echo -e "${RED}Some checks failed!${NC}"
fi

exit $OVERALL_STATUS

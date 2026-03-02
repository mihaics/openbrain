#!/bin/bash
# Open Brain Backup Script
# Automated backups for Open Brain database

set -e

# Configuration
BACKUP_DIR="${BACKUP_DIR:-./data/backups}"
DB_NAME="${DB_NAME:-openbrain}"
DB_USER="${DB_USER:-postgres}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Generate timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/openbrain_${TIMESTAMP}.sql"

echo "Starting backup..."
echo "Database: $DB_NAME"
echo "Backup file: $BACKUP_FILE"

# Perform backup
if command -v pg_dump &> /dev/null; then
    PGPASSWORD="$DB_PASSWORD" pg_dump \
        -h "$DB_HOST" \
        -p "$DB_PORT" \
        -U "$DB_USER" \
        -f "$BACKUP_FILE" \
        "$DB_NAME"
    
    # Compress backup
    gzip "$BACKUP_FILE"
    BACKUP_FILE="${BACKUP_FILE}.gz"
    
    echo "Backup completed: $BACKUP_FILE"
    
    # Get backup size
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "Backup size: $SIZE"
else
    echo "Error: pg_dump not found. Please install PostgreSQL client."
    exit 1
fi

# Clean old backups
echo "Cleaning old backups (retention: $RETENTION_DAYS days)..."
find "$BACKUP_DIR" -name "openbrain_*.sql.gz" -mtime +$RETENTION_DAYS -delete

# Count remaining backups
BACKUP_COUNT=$(find "$BACKUP_DIR" -name "openbrain_*.sql.gz" | wc -l)
echo "Remaining backups: $BACKUP_COUNT"

# Optional: Upload to remote storage (S3, etc.)
if [ -n "$BACKUP_S3_BUCKET" ]; then
    echo "Uploading to S3..."
    aws s3 cp "$BACKUP_FILE" "s3://$BACKUP_S3_BUCKET/backups/"
    echo "Uploaded to S3"
fi

echo "Backup script completed successfully!"

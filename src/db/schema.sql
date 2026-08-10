-- Open Brain Database Schema
-- PostgreSQL + pgvector

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- Core memory table
CREATE TABLE IF NOT EXISTS memory (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source VARCHAR(50) NOT NULL,
    source_id VARCHAR(255),
    content TEXT NOT NULL,
    raw_content TEXT,
    embedding vector(1024),
    entities JSONB DEFAULT '{}',
    tags TEXT[] DEFAULT '{}',
    tag_sources JSONB DEFAULT '{}',
    importance FLOAT DEFAULT 0.5,
    created_at TIMESTAMP DEFAULT NOW(),
    original_date TIMESTAMP,
    language VARCHAR(10),
    metadata JSONB DEFAULT '{}',
    -- Access tracking. Incremented only on an explicit fetch-by-id, never on
    -- search: appearing in a result list is not evidence a memory was used,
    -- and counting it would let search reinforce its own results.
    access_count INT NOT NULL DEFAULT 0,
    last_accessed_at TIMESTAMP
);

-- Capture state: a singleton guarding auto-capture against a database
-- restore. The generation here is compared against a filesystem checkpoint;
-- a mismatch the committed work cannot explain means the database was rolled
-- back underneath the capture ledger, and draining must stop rather than
-- silently skip the rows the restore lost.
--
-- PRIMARY KEY, not just CHECK (id = 1): the check alone permits many rows all
-- holding id = 1.
CREATE TABLE IF NOT EXISTS capture_state (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    generation BIGINT NOT NULL DEFAULT 1,
    previous_generation BIGINT,
    committed_work_identity JSONB,
    -- Separate from `generation` on purpose: bumping the main generation for a
    -- reprocess would trip the restore interlock, and reusing
    -- committed_work_identity would destroy normal crash-recovery evidence.
    reprocess_counter BIGINT NOT NULL DEFAULT 0,
    active_reprocess JSONB,
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

INSERT INTO capture_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_memory_embedding ON memory USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memory_entities ON memory USING gin (entities);
CREATE INDEX IF NOT EXISTS idx_memory_tags ON memory USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_memory_source ON memory (source);
CREATE INDEX IF NOT EXISTS idx_memory_created ON memory (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_original_date ON memory (original_date DESC);

-- Full-text search index
CREATE INDEX IF NOT EXISTS idx_memory_content_fts ON memory USING GIN (to_tsvector('english', content));

-- Function to generate embeddings (placeholder - computed in Python)
-- This allows us to store pre-computed embeddings

-- Function to get memories from today
CREATE OR REPLACE FUNCTION get_today_memories(limit_count INTEGER DEFAULT 10)
RETURNS TABLE (
    id UUID,
    source VARCHAR,
    content TEXT,
    tags TEXT[],
    created_at TIMESTAMP,
    importance FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT m.id, m.source, m.content, m.tags, m.created_at, m.importance
    FROM memory m
    WHERE m.created_at >= CURRENT_DATE
    ORDER BY m.importance DESC, m.created_at DESC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql;

-- Function to get memory statistics
CREATE OR REPLACE FUNCTION get_memory_stats()
RETURNS TABLE (
    total_count BIGINT,
    source_name VARCHAR,
    source_count BIGINT,
    tag_name TEXT,
    tag_count BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        COUNT(*)::BIGINT as total_count,
        NULL::VARCHAR as source_name,
        NULL::BIGINT as source_count,
        NULL::TEXT as tag_name,
        NULL::BIGINT as tag_count
    FROM memory;
END;
$$ LANGUAGE plpgsql;

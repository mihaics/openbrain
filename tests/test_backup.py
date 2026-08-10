"""Tests for the backup command."""
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


REAL_TOC = "\n".join([
    "3798; 0 0 ACL - SCHEMA public pg_database_owner",
    "3; 3079 16386 EXTENSION - uuid-ossp ",
    '3799; 0 0 COMMENT - EXTENSION "uuid-ossp" ',
    "2; 3079 16397 EXTENSION - vector ",
    "3800; 0 0 COMMENT - EXTENSION vector ",
    "303; 1255 16725 FUNCTION public get_memory_stats() openbrain",
    "324; 1255 16726 FUNCTION public get_today_memories(integer) openbrain",
    "221; 1259 16727 TABLE public memory openbrain",
    "3791; 0 16727 TABLE DATA public memory openbrain",
    "3643; 2606 16995 CONSTRAINT public memory memory_pkey openbrain",
    "3635; 1259 16996 INDEX public idx_memory_content_fts openbrain",
    "3636; 1259 16997 INDEX public idx_memory_created openbrain",
    "3637; 1259 16998 INDEX public idx_memory_embedding openbrain",
    "3638; 1259 16999 INDEX public idx_memory_entities openbrain",
    "3639; 1259 17000 INDEX public idx_memory_original_date openbrain",
    "3640; 1259 17001 INDEX public idx_memory_source openbrain",
    "3641; 1259 17002 INDEX public idx_memory_tags openbrain",
    "2279; 826 17003 DEFAULT ACL public DEFAULT PRIVILEGES FOR SEQUENCES postgres",
    "2280; 826 17004 DEFAULT ACL public DEFAULT PRIVILEGES FOR TABLES postgres",
])


class TestVerification:
    def test_accepts_the_real_toc(self, tmp_path):
        from src.cli.backup import verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")

        with patch('src.cli.backup._pg_restore_list', return_value=REAL_TOC), \
             patch('src.cli.backup._pg_restore_decode') as mock_decode:
            verify_archive(archive)

        mock_decode.assert_called_once()

    def test_rejects_missing_index(self, tmp_path):
        """Completeness: an archive missing one index must fail even though it
        still has TABLE DATA."""
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")
        partial_toc = "\n".join(
            l for l in REAL_TOC.splitlines() if "idx_memory_tags" not in l
        )

        with patch('src.cli.backup._pg_restore_list', return_value=partial_toc), \
             patch('src.cli.backup._pg_restore_decode'):
            with pytest.raises(VerificationError, match="missing expected"):
                verify_archive(archive)

    def test_prefix_lookalike_is_rejected(self, tmp_path):
        """Exact matching: idx_memory_tags_extra must not pass as
        idx_memory_tags."""
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")
        toc = REAL_TOC + (
            "\n9999; 1259 99999 INDEX public idx_memory_tags_extra openbrain"
        )

        with patch('src.cli.backup._pg_restore_list', return_value=toc), \
             patch('src.cli.backup._pg_restore_decode'):
            with pytest.raises(VerificationError, match="idx_memory_tags_extra"):
                verify_archive(archive)

    def test_requires_table_data_entry(self, tmp_path):
        """An archive with schema but no row data must fail."""
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")
        toc = "\n".join(
            l for l in REAL_TOC.splitlines() if "TABLE DATA" not in l
        )

        with patch('src.cli.backup._pg_restore_list', return_value=toc), \
             patch('src.cli.backup._pg_restore_decode'):
            with pytest.raises(VerificationError, match="TABLE DATA"):
                verify_archive(archive)

    def test_rejects_unexpected_toc_entry(self, tmp_path):
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")

        toc = (
            "3791; 0 16727 TABLE DATA public memory openbrain\n"
            "999; 1259 99999 TABLE public surprise_table openbrain\n"
        )
        with patch('src.cli.backup._pg_restore_list', return_value=toc), \
             patch('src.cli.backup._pg_restore_decode'):
            with pytest.raises(VerificationError, match="surprise_table"):
                verify_archive(archive)

    def test_payload_decode_failure_propagates(self, tmp_path):
        """Must use the complete TOC: with exact-set validation, a one-line TOC
        fails on 'missing expected objects' before decode is ever reached, so
        the test would pass for the wrong reason."""
        from src.cli.backup import VerificationError, verify_archive

        archive = tmp_path / "a.dump"
        archive.write_bytes(b"fake")

        with patch('src.cli.backup._pg_restore_list', return_value=REAL_TOC), \
             patch('src.cli.backup._pg_restore_decode',
                   side_effect=VerificationError("corrupt data block")):
            with pytest.raises(VerificationError, match="corrupt"):
                verify_archive(archive)

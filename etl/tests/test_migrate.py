from __future__ import annotations

from pathlib import Path

import pytest

from flowetl.migrate import Migration, discover, split_statements

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "clickhouse" / "migrations"


def test_split_ignores_comments_and_blank_statements() -> None:
    sql = """
    -- a comment; with a semicolon
    CREATE TABLE a (x UInt8) ENGINE = Memory;

    CREATE TABLE b (x UInt8) ENGINE = Memory;
    """
    assert split_statements(sql) == [
        "CREATE TABLE a (x UInt8) ENGINE = Memory",
        "CREATE TABLE b (x UInt8) ENGINE = Memory",
    ]


def test_repo_migrations_are_well_formed() -> None:
    migrations = discover(REPO_MIGRATIONS)
    versions = [m.version for m in migrations]
    assert versions == sorted(versions) == list(range(1, len(versions) + 1))
    for m in migrations:
        assert m.statements(), f"{m.name} has no statements"
        for stmt in m.statements():
            if stmt.upper().startswith(
                (
                    "CREATE TABLE",
                    "CREATE DICTIONARY",
                    "CREATE DATABASE",
                    "CREATE MATERIALIZED VIEW",
                    "CREATE VIEW",
                )
            ):
                assert "ON CLUSTER" in stmt, f"{m.name}: DDL must run on the whole cluster"
                assert "IF NOT EXISTS" in stmt, f"{m.name}: DDL must be re-runnable"


def test_checksum_changes_with_content() -> None:
    a = Migration(1, "x", "SELECT 1")
    assert a.checksum != Migration(1, "x", "SELECT 2").checksum


def test_bad_names_and_duplicates_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "1_bad.sql").write_text("SELECT 1")
    with pytest.raises(ValueError, match="bad migration file name"):
        discover(tmp_path)
    (tmp_path / "1_bad.sql").unlink()
    (tmp_path / "0001_a.sql").write_text("SELECT 1")
    (tmp_path / "0001_b.sql").write_text("SELECT 1")
    with pytest.raises(ValueError, match="duplicate"):
        discover(tmp_path)

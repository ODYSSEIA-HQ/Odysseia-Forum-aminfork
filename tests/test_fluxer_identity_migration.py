"""真实 PostgreSQL 小数据验证在线迁移，不访问应用数据库。"""

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from alembic.migration import MigrationContext
from alembic.operations import Operations
from identity.migration_service import MigrationService
from identity.reference_manifest import REFERENCES


@pytest.fixture
def migration_engine():
    """仅使用显式指定的临时数据库和独立随机 schema。"""
    url = os.environ.get("FLUXER_MIGRATION_TEST_URL")
    if not url:
        pytest.skip("需要显式临时 PostgreSQL FLUXER_MIGRATION_TEST_URL")
    schema = "test_fluxer_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as conn:
            for table, columns in REFERENCES.items():
                fields = ["id BIGSERIAL PRIMARY KEY"] if table not in {"author", "tag_vote"} else []
                fields += [
                    f"{column} BIGINT" + (" PRIMARY KEY" if table == "author" else "")
                    for column in columns
                ]
                if table == "tag_vote":
                    fields += ["binding_id BIGINT NOT NULL", "PRIMARY KEY (binding_id,user_id)"]
                if table == "user_search_preferences":
                    fields += ["include_authors JSON", "exclude_authors JSON"]
                conn.execute(text(f"CREATE TABLE {table} ({','.join(fields)})"))
            path = (
                Path(__file__).parents[1]
                / "alembic/versions/fluxer_identity_prepare.py"
            )
            spec = importlib.util.spec_from_file_location("prepare", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.op = Operations(MigrationContext.configure(conn))
            module.upgrade()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def members():
    """刻意让两个身份空间数值交叉，避免错误碰撞归属。"""
    return [
        {"discord_user_id": "100", "user_id": "200", "username": "alice"},
        {"discord_user_id": "200", "user_id": "100", "username": "bob"},
    ]


def test_prepare_preserves_legacy_and_captures_concurrent_writers(migration_engine):
    """旧写入、条件回填和同值跨空间 ID 不会混淆作者。"""
    service = MigrationService(migration_engine)
    with migration_engine.begin() as conn:
        conn.execute(text("INSERT INTO author(id) VALUES(100),(200),(0)"))
        conn.execute(text("INSERT INTO thread(author_id) VALUES(100),(999),(0),(NULL)"))
    service.import_batch(members())
    assert service.backfill_batch("thread", "author_id", 1) == 1
    with migration_engine.begin() as conn:
        assert conn.execute(
            text("SELECT author_id,fluxer_author_id FROM thread ORDER BY id LIMIT 1")
        ).one() == (100, "200")
        conn.execute(text("UPDATE thread SET author_id=200 WHERE id=1"))
        assert (
            conn.execute(
                text("SELECT fluxer_author_id FROM thread WHERE id=1")
            ).scalar_one()
            == "100"
        )
        conn.execute(text("INSERT INTO thread(author_id) VALUES(100)"))
        assert (
            conn.execute(
                text("SELECT fluxer_author_id FROM thread ORDER BY id DESC LIMIT 1")
            ).scalar_one()
            == "200"
        )
    assert service.audit()["thread.author_id"] == 1
    service.import_batch(
        [{"discord_user_id": "999", "user_id": "777", "username": "late"}]
    )
    for table, columns in REFERENCES.items():
        for column in columns:
            service.backfill_batch(table, column, 20)
    assert not any(service.audit().values())


def test_backfill_skips_locked_rows_and_restarts(migration_engine):
    """回填不等待热写入行，下一轮会收敛到并发修改后的源身份。"""
    service = MigrationService(migration_engine)
    with migration_engine.begin() as conn:
        conn.execute(text("INSERT INTO thread(author_id) VALUES(100),(100)"))
    service.import_batch(members())
    with migration_engine.begin() as writer:
        writer.execute(text("SELECT id FROM thread WHERE id=1 FOR UPDATE"))
        assert service.backfill_batch("thread", "author_id", 10) == 1
        writer.execute(text("UPDATE thread SET author_id=200 WHERE id=1"))
    assert service.backfill_batch("thread", "author_id", 10) == 0
    with migration_engine.connect() as conn:
        assert conn.execute(
            text("SELECT fluxer_author_id FROM thread ORDER BY id")
        ).scalars().all() == ["100", "200"]


def test_lists_batches_preserve_order_and_block_missing_mapping(migration_engine):
    """超过一批的列表完整回填，映射缺失不能默默截断。"""
    service = MigrationService(migration_engine)
    service.import_batch(members())
    with migration_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO user_search_preferences(user_id,include_authors) VALUES(100,'[100,200,100]'),(200,'[200]'),(100,'[999]')"
            )
        )
    assert service.backfill_lists("user_search_preferences", "include_authors", 1) == 1
    assert service.backfill_lists("user_search_preferences", "include_authors", 1) == 1
    assert service.backfill_lists("user_search_preferences", "include_authors", 1) == 0
    with migration_engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT fluxer_include_authors FROM user_search_preferences WHERE id=1"
            )
        ).scalar_one() == ["200", "100", "200"]
    assert service.audit()["user_search_preferences.include_authors"] == 1


def test_import_is_restartable_and_rejects_identity_transfer(migration_engine):
    """权威输入重复导入幂等，历史所有权不能通过重绑静默转移。"""
    service = MigrationService(migration_engine)
    assert service.import_batch(members()) == 2
    assert service.import_batch(members()) == 2
    with pytest.raises(ValueError, match="绑定冲突"):
        service.import_batch(
            [{"discord_user_id": "100", "user_id": "300", "username": "intruder"}]
        )
    with pytest.raises(Exception):
        service.import_batch(
            [{"discord_user_id": "300", "user_id": "200", "username": "collision"}]
        )
    with migration_engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM "user"')).scalar_one() == 2

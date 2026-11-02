"""真实 PostgreSQL 的多历史身份导入及防回退验证。"""

# ruff: noqa: F811
import importlib.util
from pathlib import Path
import pytest
from sqlalchemy import text
from alembic.migration import MigrationContext
from alembic.operations import Operations
from identity.migration_service import MigrationService
from test_fluxer_identity_migration import migration_engine  # noqa: F401


def apply_history(engine, action="upgrade"):
    """在测试随机schema应用真实历史身份DDL。"""
    path = Path(__file__).parents[1] / "alembic/versions/fluxer_identity_history.py"
    spec = importlib.util.spec_from_file_location("history_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        getattr(module, action)()


def test_distinct_profile_and_multiple_history_rows_do_not_reactivate_stale_binding(
    migration_engine,
):
    """同批多历史绑定只upsert一次资料，旧快照不复活登录。"""
    apply_history(migration_engine)
    service = MigrationService(migration_engine)
    service.import_batch(
        [
            {
                "user_id": "500",
                "discord_user_id": "101",
                "username": "old",
                "version": 1,
                "is_current": False,
            },
            {
                "user_id": "500",
                "discord_user_id": "102",
                "username": "current",
                "version": 3,
                "is_current": True,
            },
            {
                "user_id": "500",
                "discord_user_id": "103",
                "username": "history",
                "version": 2,
                "is_current": False,
            },
        ]
    )
    service.import_batch(
        [
            {
                "user_id": "500",
                "discord_user_id": "101",
                "username": "stale",
                "version": 1,
                "is_current": True,
            }
        ]
    )
    with migration_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT discord_user_id,is_current FROM discord_identity ORDER BY discord_user_id"
            )
        ).all()
        assert rows == [("101", False), ("102", True), ("103", False)]
        assert (
            connection.execute(
                text('SELECT username FROM "user" WHERE id=:id'), {"id": "500"}
            ).scalar_one()
            == "current"
        )
    with pytest.raises(ValueError, match="多个当前绑定"):
        service.import_batch(
            [
                {
                    "user_id": "600",
                    "discord_user_id": "104",
                    "username": "one",
                    "version": 4,
                    "is_current": True,
                },
                {
                    "user_id": "600",
                    "discord_user_id": "105",
                    "username": "two",
                    "version": 5,
                    "is_current": True,
                },
            ]
        )


def test_downgrade_checks_history_before_changing_any_data(migration_engine):
    """存在历史绑定时拒绝降级且原绑定与约束保持完整。"""
    apply_history(migration_engine)
    service = MigrationService(migration_engine)
    service.import_batch(
        [
            {
                "user_id": "500",
                "discord_user_id": "101",
                "username": "history",
                "version": 1,
                "is_current": False,
            }
        ]
    )
    with pytest.raises(RuntimeError, match="历史"):
        apply_history(migration_engine, "downgrade")
    with migration_engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT is_current FROM discord_identity WHERE discord_user_id='101'"
                )
            ).scalar_one()
            is False
        )

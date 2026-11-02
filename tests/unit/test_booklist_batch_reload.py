"""用真实 ORM 和 SQLite 隔离验证书单批量回读，不依赖全站 PostgreSQL 初始化。"""

import ast
import importlib.util
import logging
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, Table, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import registry


@pytest_asyncio.fixture
async def batch_repository():
    """加载真实新增方法和项目模型，隔离无关的全文搜索及外部服务依赖。"""
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "src"))
    module_name = f"batch_reload_booklist_item_{uuid4().hex}"
    previous_module = sys.modules.get(module_name)
    spec = importlib.util.spec_from_file_location(
        module_name, root / "src/models/booklist_item.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    existing_model_module = sys.modules.get("models.booklist_item")
    if existing_model_module is None:
        spec.loader.exec_module(module)
    item_model = (existing_model_module or module).BooklistItem
    # 书单只用到这三个字段；项目表和提交过期行为使用真实实现。
    mapper_registry = registry()
    booklist_model = type("BatchReloadBooklist", (), {})
    booklist_table = Table(
        "booklist",
        mapper_registry.metadata,
        Column("id", Integer, primary_key=True),
        Column("owner_id", Integer),
        Column("item_count", Integer),
        Column("merged_into_id", Integer),
    )
    mapper_registry.map_imperatively(booklist_model, booklist_table)
    source_path = root / "src/core/booklist_repository.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    definition = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    definition.body = [
        node
        for node in definition.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"__init__", "get_booklist", "add_threads_to_booklist"}
    ]
    namespace = {
        "Booklist": booklist_model,
        "BooklistItem": item_model,
        "select": select,
        "func": func,
        "logger": logging.getLogger(__name__),
    }
    isolated = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            definition,
        ],
        type_ignores=[],
    )
    exec(  # noqa: S102 — 仅执行仓库固定文件中的真实方法，避免替身掩盖 ORM 过期问题。
        compile(ast.fix_missing_locations(isolated), str(source_path), "exec"),
        namespace,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(mapper_registry.metadata.create_all)
            await connection.run_sync(item_model.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=True)
        async with factory() as session:
            session.add(booklist_model(id=1, owner_id=42, item_count=0))
            await session.commit()
            yield namespace["BooklistRepository"](session), engine, item_model
    finally:
        await engine.dispose()
        mapper_registry.dispose()
        if existing_model_module is None:
            item_model.metadata.remove(item_model.__table__)
        sys.path.remove(str(root / "src"))
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1, 500, 501, 1000])
async def test_batch_reload_query_count_and_fields(batch_repository, size):
    """验证回读次数按批次增长，提交后全部字段、请求顺序和计数保持正确。"""
    repository, engine, _ = batch_repository
    statements = []

    def listener(connection, cursor, statement, parameters, context, many):
        """记录本测试连接执行的语句数量。"""
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", listener)
    participated_at = datetime(2026, 10, 24, 12, 0)  # noqa: DTZ001 — 模型使用无时区 UTC。
    thread_ids = list(reversed(range(10000, 10000 + size)))
    items = [
        SimpleNamespace(
            thread_id=thread_id,
            comment=f"备注 {thread_id}",
            display_order=None,
            tournament_participated_at=participated_at,
        )
        for thread_id in thread_ids
    ]
    try:
        added = await repository.add_threads_to_booklist(1, items)
        # 排除提交前的存在性查询，统计实际项目全字段回读 SQL。
        reloads = [
            sql for sql in statements if sql.startswith("SELECT booklist_item.id")
        ]
        assert len(reloads) == (size + 499) // 500
        assert [item.thread_id for item in added] == thread_ids
        assert [item.display_order for item in added] == list(range(1, size + 1))
        assert all(
            item.id and item.owner_id == 42 and item.booklist_id == 1 for item in added
        )
        assert all(item.tournament_participated_at == participated_at for item in added)
        assert all(
            item.created_at and item.updated_at and item.display_message_id is None
            for item in added
        )
        assert [item.comment for item in added] == [item.comment for item in items]
        assert (await repository.get_booklist(1)).item_count == size
        # 重复加入已有项目不增加计数，也不回读项目实体。
        statements.clear()
        assert await repository.add_threads_to_booklist(1, items) == []
        assert not [
            sql for sql in statements if sql.startswith("SELECT booklist_item.id")
        ]
        assert (await repository.get_booklist(1)).item_count == size
        await repository.session.close()
        assert [item.thread_id for item in added] == thread_ids
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listener)


@pytest.mark.asyncio
async def test_batch_reload_rolls_back_failed_insert(batch_repository):
    """验证刷新前的插入失败仍回滚项目及书单计数且会话可以继续使用。"""
    repository, _, item_model = batch_repository
    # 让约束失败发生在本次新增的显式 flush 中，验证其异常回滚边界。
    repository.session.autoflush = False
    duplicate = SimpleNamespace(
        thread_id=88,
        comment=None,
        display_order=7,
        tournament_participated_at=None,
    )
    with pytest.raises(IntegrityError):
        await repository.add_threads_to_booklist(1, [duplicate, duplicate])
    assert (await repository.get_booklist(1)).item_count == 0
    assert (
        await repository.session.execute(select(func.count()).select_from(item_model))
    ).scalar_one() == 0
    assert await repository.add_threads_to_booklist(1, []) == []
    added = await repository.add_threads_to_booklist(1, [duplicate])
    assert len(added) == 1
    assert added[0].thread_id == 88
    assert added[0].display_order == 7

"""验证无目标行锁时原生标签及来源仍来自同一个查询快照。"""

import ast
import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import Column, Integer, String, Table, func, select
from sqlalchemy.orm import registry


class TestTagSnapshot(unittest.IsolatedAsyncioTestCase):
    """直接执行真实快照方法并模拟原生绑定在后续查询前移除。"""

    def prepare_snapshot(self, kind="thread"):
        """构造真实查询模型及可在查询间改变原生绑定的内存状态。"""
        mapper_registry = registry()
        models = {}
        for name, columns in {
            "Tag": ("source", "deleted_at"),
            "TagBinding": (
                "tag_id",
                "target_type",
                "target_id",
                "binding_source",
                "discord_source_id",
                "ended_at",
                "created_at",
            ),
            "TagVote": ("binding_id", "user_id"),
            "TagRelation": ("kind", "source_id", "target_id"),
        }.items():
            model = type(name, (), {})
            table = Table(
                name.lower(),
                mapper_registry.metadata,
                Column("id", Integer, primary_key=True),
                *(Column(column, String) for column in columns),
            )
            mapper_registry.map_imperatively(model, table)
            models[name] = model
        tag = models["Tag"](id=3, source="discord", deleted_at=None)
        database_native = [(tag, 71)] if kind == "thread" else []
        history = (11, None, None)
        session = MagicMock()
        native_result = SimpleNamespace(all=lambda: list(database_native))
        history_result = SimpleNamespace(one=lambda: history)
        session.execute = AsyncMock(
            side_effect=[native_result, history_result]
            if kind == "thread"
            else [history_result]
        )
        repository = SimpleNamespace(rows=AsyncMock(return_value=[]))
        # 原生查询完成后模拟另一个事务移除绑定，后续独立来源查询将得到空集。
        load_sources = AsyncMock(
            side_effect=lambda *args: database_native.clear() or {}
        )
        namespace = dict(
            models,
            select=select,
            func=func,
            hashlib=hashlib,
            load_discord_sources=load_sources,
            is_fluxer_mode=lambda: False,
            uses_canonical_storage=lambda: False,
            resolve_business_id=AsyncMock(
                side_effect=lambda _session, actor, **kwargs: actor
            ),
        )
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse(
            (root / "src/tag/custom_tag_service.py").read_text(encoding="utf-8")
        )
        definition = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        definition.body = [
            node
            for node in definition.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "snapshot"
        ]
        module = ast.fix_missing_locations(
            ast.Module(body=[definition], type_ignores=[])
        )
        # 固定仓库路径的定义没有导入或执行外部输入。
        exec(compile(module, "custom_tag_service.py", "exec"), namespace)  # noqa: S102
        service = namespace["CustomTagService"]()
        service.session = session
        service.bindings = AsyncMock(return_value={})
        service.repo = MagicMock(return_value=repository)
        service.tag_cache = {}
        service.source_cache = {}
        service.logical_target = lambda model, kind, target: (
            model.target_type == kind
        ) & (model.target_id == target)
        service.tag_data = lambda value: {"id": str(value.id)}
        return service, models, session, database_native, history

    async def test_native_removal_between_queries_keeps_source_from_joined_row(self):
        """后续查询前移除绑定不会造成来源字典缺键，原生字段和版本格式保持一致。"""
        service, models, session, database_native, history = self.prepare_snapshot()
        result = await service.snapshot(
            5, "thread", 10, SimpleNamespace(native_tag_revision=9)
        )
        self.assertEqual(database_native, [])
        self.assertEqual(
            result["tags"],
            [
                {
                    "id": "3",
                    "readonly": True,
                    "binding_source": "discord_sync",
                    "discord_source_id": "71",
                }
            ],
        )
        expected_version = hashlib.sha256(f"n:3|{history}|9".encode()).hexdigest()
        self.assertEqual(result["version"], expected_version)
        self.assertFalse(result["over_limit"])
        self.assertEqual(result["conflicting_pairs"], [])
        # 原生标签和来源列同一 JOIN 读取，之后不再通过仓储单独查询绑定。
        statement = str(session.execute.await_args_list[0].args[0])
        self.assertIn("tagbinding.discord_source_id", statement)
        self.assertIn("JOIN tagbinding", statement)
        self.assertNotIn(
            models["TagBinding"], [call.args[0] for call in service.repo.call_args_list]
        )
        self.assertEqual(session.execute.await_count, 2)

    async def test_booklist_snapshot_does_not_query_native_sources(self):
        """书单保持空原生集合及既有版本组成，不引入线程原生查询。"""
        service, models, session, database_native, history = self.prepare_snapshot(
            "booklist"
        )
        result = await service.snapshot(5, "booklist", 10, SimpleNamespace())
        self.assertEqual(result["tags"], [])
        self.assertEqual(database_native, [])
        self.assertEqual(session.execute.await_count, 1)
        self.assertEqual(
            result["version"], hashlib.sha256(f"{history}|0".encode()).hexdigest()
        )
        self.assertFalse(result["over_limit"])
        self.assertEqual(result["conflicting_pairs"], [])
        self.assertNotIn(
            models["TagBinding"], [call.args[0] for call in service.repo.call_args_list]
        )


if __name__ == "__main__":
    unittest.main()

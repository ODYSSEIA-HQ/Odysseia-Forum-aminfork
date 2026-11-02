"""原生主帖标签快照与统一概念投影。"""

from sqlalchemy import select, delete, text
from sqlalchemy.dialects.postgresql import insert
from models.tag import Tag
from models.work_native_tag_source import WorkNativeTagSource
from models.work_native_tag_binding import WorkNativeTagBinding


class WorkNativeTagService:
    """只更新原生来源绑定，不改本地挂标轮次和票数。"""

    def __init__(self, session):
        """复用来源事件事务。"""
        self.session = session

    async def apply(self, thread, event):
        """缺少 tags 字段不清空，显式空列表移除全部该主来源绑定。"""
        if thread.role != "main" or "tags" not in event:
            return
        snapshots = event["tags"]
        if not isinstance(snapshots, list):
            raise ValueError("原生标签快照必须是列表")
        names = {item["name"] for item in snapshots}
        # 同名概念创建串行化，避免来自不同作品的并发首次快照冲突。
        for name in sorted(names):
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "native-tag:fluxer:" + name},
            )
        concepts = {
            tag.name: tag
            for tag in (
                await self.session.execute(
                    select(Tag).where(
                        Tag.source == "fluxer",
                        Tag.name.in_(names),
                        Tag.deleted_at.is_(None),
                    )
                )
            ).scalars()
        }
        for name in sorted(names - concepts.keys()):
            tag = Tag(name=name, source="fluxer")
            self.session.add(tag)
            await self.session.flush()
            concepts[name] = tag
        sources = (
            (
                await self.session.execute(
                    select(WorkNativeTagSource).where(
                        WorkNativeTagSource.platform == thread.platform,
                        WorkNativeTagSource.guild_id == str(thread.guild_id),
                        WorkNativeTagSource.channel_id == str(thread.channel_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_external = {source.external_id: source for source in sources}
        desired = []
        for item in snapshots:
            source = by_external.get(str(item["id"]))
            if source is None:
                source = WorkNativeTagSource(
                    platform=thread.platform,
                    guild_id=str(thread.guild_id),
                    channel_id=str(thread.channel_id),
                    external_id=str(item["id"]),
                    tag_id=concepts[item["name"]].id,
                    name=item["name"],
                    emoji=item.get("emoji") or {},
                )
                self.session.add(source)
            else:
                source.tag_id = concepts[item["name"]].id
                source.name = item["name"]
                source.emoji = item.get("emoji") or {}
            await self.session.flush()
            desired.append(source.id)
        await self.session.execute(
            delete(WorkNativeTagBinding).where(
                WorkNativeTagBinding.work_id == thread.work_id
            )
        )
        if desired:
            await self.session.execute(
                insert(WorkNativeTagBinding),
                [
                    {"work_id": thread.work_id, "source_id": source_id}
                    for source_id in set(desired)
                ],
            )

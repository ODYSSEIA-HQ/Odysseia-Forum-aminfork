"""/api/v1 层的种子数据：契约测试账号、tag 目录（内容参考 web 仓库 src/mock/seed.ts）。

用法（库已 alembic upgrade head）：
    DATABASE_URL=postgresql+asyncpg://odysseia:changeme@localhost:5432/odysseia_dev \\
    FLUXER_FAKE=1 uv run python scripts/seed_site_v1.py

- 幂等：重复跑会更新已有行，不会重复插入。
- 账号只在 FLUXER_FAKE=1 时建（真实环境的账号在 Fluxer 里，密码 Forum 不持有）；
  真实环境下只给已存在的同名账号补站内状态（资格、权限、新人期）。
- 预置作品（seed_works）：已发布全年龄作品（最新版本带文件）、R18 作品（R18 封面）。
"""

import asyncio
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402

import models  # noqa: E402,F401
from models.discord_identity import DiscordIdentity  # noqa: E402
from models.site_account import (  # noqa: E402
    SiteAccount,
    SiteRestrictedAccess,
    SiteSetting,
    SiteUserPrefs,
)
from models.site_tag import SiteTag, SiteTagGroup, SiteTagQuestion  # noqa: E402
from models.user import User  # noqa: E402
from shared.database import AsyncSessionFactory  # noqa: E402
from shared.time_utils import utc_now  # noqa: E402
from site_v1.config import get_config  # noqa: E402
from site_v1.services.accounts import ensure_account_rows  # noqa: E402

PASSWORD = "demo1234"
CATALOG_VERSION = "2026-10-18.1"

ALL_PERMISSIONS = [
    "site.admin",
    "roles.edit_decor",
    "roles.grant_decor",
    "roles.edit_perm",
    "roles.lock",
    "works.manage_any",
    "tags.manage",
    "moderation.images",
    "moderation.content",
    "moderation.reports",
    "announcements.publish",
    "users.ban",
    "users.restricted_access",
    "chat.attach",
    "chat.poll",
    "chat.fun",
    "home.manage",
    "banners.review",
    "events.manage",
]

# username, 显示名, R18 资格, 新人, 额外权限, 假 Discord ID
ACCOUNTS = [
    ("demo", "演示站长", "granted", False, ALL_PERMISSIONS, "900000000000000001"),
    ("newbie", "新来的", "none", True, [], None),
    ("lin", "林", "granted", False, [], "900000000000000003"),
]

RESOURCE_TYPES = ["角色卡", "世界书", "预设", "美化", "插件", "工具", "项目", "教程"]

# (组 ID, 名字, 对所有类型必选, maxSelect, onlyForTypes, requiredForTypes, tags)
# tag 写法：名字字符串，或 dict(name, restricts, rating, forTypes)
GROUPS = [
    (
        "rating",
        "分级",
        True,
        1,
        None,
        None,
        [
            {"name": "全年龄", "rating": "general"},
            {"name": "R18", "restricts": True, "rating": "r18"},
            {"name": "R18G", "restricts": True, "rating": "r18g"},
        ],
    ),
    ("type", "资源类型", True, 1, None, None, RESOURCE_TYPES),
    (
        "audience",
        "受众",
        False,
        1,
        ["角色卡"],
        ["角色卡"],
        ["男性向", "女性向", "全性向", "其他向"],
    ),
    (
        "form",
        "形态",
        False,
        1,
        None,
        None,
        ["纯文字", "轻前端·美化", "重前端·独立前端"],
    ),
    (
        "theme",
        "题材",
        False,
        3,
        None,
        None,
        [
            "纯爱",
            "百合",
            "后宫",
            "人外",
            "古风",
            "现代",
            "幻想",
            "系统流",
            "神人",
            "数据删除",
        ],
    ),
    (
        "function",
        "功能",
        False,
        15,
        ["世界书", "工具", "插件", "项目"],
        None,
        [
            *(
                {"name": n, "forTypes": ["世界书"]}
                for n in ["世界观", "人物设定", "功能性", "金手指", "规则系", "文风"]
            ),
            *(
                {"name": n, "forTypes": ["工具"]}
                for n in ["反向代理", "脚本工具", "Discord BOT", "写卡工具"]
            ),
            *({"name": n, "forTypes": ["插件"]} for n in ["酒馆扩展", "QuickReplies"]),
            {"name": "正则", "forTypes": ["工具", "插件"]},
            *({"name": n, "forTypes": ["项目"]} for n in ["游戏", "前端", "开发记录"]),
        ],
    ),
    (
        "warning",
        "内容提示",
        False,
        15,
        None,
        None,
        [
            "NTR",
            "NTL",
            "乱伦",
            "支配",
            "调教",
            "SM",
            "昆虫",
            "密集",
            {"name": "破限", "restricts": True},
        ],
    ),
    (
        "attribute",
        "属性",
        False,
        15,
        None,
        None,
        [
            "原创",
            "同人",
            "搬运",
            "汉化",
            "女性视角",
            "御姐",
            "病娇",
            "多角色",
            "多路线",
            "工具卡",
        ],
    ),
]

QUESTIONS = [
    {
        "id": "jailbreak",
        "text": "这个预设是否含破限内容？",
        "hint": "「破限」指用提示词绕过模型自带的安全限制，让它输出原本会拒绝的内容。"
        "含破限的预设只对打开了对应模式的成员显示。",
        "forTypes": ["预设"],
        "restricts": ("warning", "破限"),
    }
]


async def seed_accounts(session_factory) -> dict[str, str]:
    """建契约测试账号，返回 {username: user_id}。"""
    config = get_config()
    ids: dict[str, str] = {}
    fake = None
    if config.fluxer_fake:
        from site_v1.fluxer.fake import FakeFluxer

        fake = FakeFluxer(session_factory=session_factory)
    now = utc_now()
    for username, display, access, newcomer, perms, discord_id in ACCOUNTS:
        if fake is not None:
            user_id = await fake.ensure_account(
                username,
                PASSWORD,
                email=f"{username}@example.com",
                display_name=display,
            )
        else:
            async with session_factory() as session:
                user_id = (
                    await session.execute(
                        select(User.id).where(User.username == username)
                    )
                ).scalar_one_or_none()
            if user_id is None:
                print(f"跳过 {username}：真实 Fluxer 环境下要先在 Fluxer 建号")
                continue
        ids[username] = user_id
        async with session_factory() as session:
            await ensure_account_rows(session, user_id, email=f"{username}@example.com")
            account = await session.get(SiteAccount, user_id)
            account.permissions = list(perms)
            account.newcomer_until = (
                now + timedelta(days=config.newcomer_days) if newcomer else None
            )
            account.quiz_passed = not newcomer
            ra = await session.get(SiteRestrictedAccess, user_id)
            ra.state = access
            ra.source = "staff" if access == "granted" else None
            ra.checked_at = now
            ra.age_confirmed_at = now if access == "granted" else None
            ra.terms_version = config.terms_version if access == "granted" else None
            ra.revoked_at = None
            ra.revoked_by = None
            ra.updated_at = now
            prefs = await session.get(SiteUserPrefs, user_id)
            prefs.content_mode = "general"
            prefs.show_r18g = False
            if discord_id and fake is not None:
                await session.execute(
                    insert(DiscordIdentity)
                    .values(
                        discord_user_id=discord_id,
                        user_id=user_id,
                        version=1,
                        is_current=True,
                    )
                    .on_conflict_do_nothing()
                )
            await session.commit()
        print(
            f"账号 {username} -> {user_id}（R18 资格 {access}{'，新人' if newcomer else ''}）"
        )
    return ids


async def seed_tag_catalog(session_factory) -> None:
    """官方 tag 组、官方 tag、必答问题；目录版本号写 site_setting[tag_catalog]。"""
    async with session_factory() as session:
        tag_ids: dict[tuple[str, str], int] = {}
        for order, (
            gid,
            name,
            required,
            max_select,
            only,
            required_for,
            tags,
        ) in enumerate(GROUPS):
            await session.execute(
                insert(SiteTagGroup)
                .values(
                    id=gid,
                    name=name,
                    required=required,
                    max_select=max_select,
                    only_for_types=only,
                    required_for_types=required_for,
                    sort_order=order,
                )
                .on_conflict_do_update(
                    index_elements=[SiteTagGroup.id],
                    set_=dict(
                        name=name,
                        required=required,
                        max_select=max_select,
                        only_for_types=only,
                        required_for_types=required_for,
                        sort_order=order,
                    ),
                )
            )
            for tag_order, raw in enumerate(tags):
                spec = {"name": raw} if isinstance(raw, str) else dict(raw)
                row = (
                    (
                        await session.execute(
                            select(SiteTag).where(
                                SiteTag.group_id == gid,
                                SiteTag.name == spec["name"],
                                SiteTag.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if row is None:
                    row = SiteTag(group_id=gid, name=spec["name"])
                    session.add(row)
                row.restricts = bool(spec.get("restricts"))
                row.rating = spec.get("rating")
                row.official = True
                row.for_types = spec.get("forTypes")
                row.sort_order = tag_order
                await session.flush()
                tag_ids[(gid, spec["name"])] = row.id
        for order, q in enumerate(QUESTIONS):
            values = dict(
                text=q["text"],
                hint=q["hint"],
                for_types=q["forTypes"],
                restricts_tag_id=tag_ids[q["restricts"]],
                sort_order=order,
            )
            await session.execute(
                insert(SiteTagQuestion)
                .values(id=q["id"], **values)
                .on_conflict_do_update(index_elements=[SiteTagQuestion.id], set_=values)
            )
        await session.execute(
            insert(SiteSetting)
            .values(
                key="tag_catalog",
                value={"version": CATALOG_VERSION},
                updated_at=utc_now(),
            )
            .on_conflict_do_update(
                index_elements=[SiteSetting.key],
                set_=dict(value={"version": CATALOG_VERSION}, updated_at=utc_now()),
            )
        )
        await session.commit()
    print(
        f"tag 目录：{len(GROUPS)} 组、{len(tag_ids)} 个官方 tag、{len(QUESTIONS)} 个必答问题"
    )


# 预置作品：(标题, 简介, 分级 tag 名, 资源类型, 其他 tag, 封面分级或 None, 版本文件名或 None, 统计)
SEED_WORKS = [
    (
        "【示例】全年龄美化包",
        "预置的全年龄作品，最新版本带一个文件（契约测试 WRK-15/16 用）",
        "全年龄",
        "美化",
        [("form", "轻前端·美化"), ("attribute", "原创")],
        "general",
        "seed-theme-v1.zip",
        {"collection_count": 12, "reaction_count": 30, "download_count": 5},
    ),
    (
        "【示例】R18 角色卡",
        "预置的 R18 作品，封面是 R18 图（契约测试 R18-15 用）",
        "R18",
        "角色卡",
        [("audience", "男性向"), ("attribute", "原创")],
        "r18",
        "seed-card-v1.png",
        {"collection_count": 8, "reaction_count": 20, "download_count": 3},
    ),
    # 每个资源类型一个全年龄作品：列表能翻页（COM-14）、tag 联想有可见作品数（TAG-17）、主榜不空
    *(
        (
            f"【示例】{type_name}",
            f"预置的全年龄{type_name}",
            "全年龄",
            type_name,
            [("audience", "全性向")] if type_name == "角色卡" else [],
            None,
            None,
            {"collection_count": 1, "reaction_count": i + 1},
        )
        for i, type_name in enumerate(RESOURCE_TYPES)
    ),
]


async def seed_works(session_factory, accounts: dict[str, str]) -> None:
    """已发布的全年龄作品（最新版本带文件）、R18 作品（R18 封面）。

    主榜（GET /boards）按 tag 目录生成（全站 + 类型/受众/形态/题材各 tag），不需要单独的数据。
    文件只插 attachment 记录（storage_key 在 seed/ 下），对象存储里的内容由上传领域决定怎么放。
    幂等：按标题找已有作品，存在就跳过。
    """
    from models.attachment import Attachment
    from models.site_tag import SiteWorkTag
    from models.work import Work
    from models.work_statistics import WorkStatistics
    from models.work_version import WorkVersion, WorkVersionFile
    from site_v1 import mode as modes

    author = accounts.get("demo")
    if author is None:
        print("跳过预置作品：没有 demo 账号")
        return
    now = utc_now()
    async with session_factory() as session:
        tags = {
            (t.group_id, t.name): t
            for t in (
                await session.execute(
                    select(SiteTag).where(SiteTag.deleted_at.is_(None))
                )
            ).scalars()
        }
        for index, (
            title,
            summary,
            rating_name,
            type_name,
            extra,
            cover_rating,
            file_name,
            stats,
        ) in enumerate(SEED_WORKS):
            exists = (
                await session.execute(
                    select(Work.id).where(
                        Work.title == title, Work.deleted_at.is_(None)
                    )
                )
            ).first()
            if exists:
                print(f"作品 {title} 已存在（{exists[0]}），跳过")
                continue
            rating_tag = tags[("rating", rating_name)]
            rating = rating_tag.rating or "general"
            is_nsfw, is_abyss = modes.rating_flags(rating)
            work = Work(
                title=title,
                summary=summary,
                description=f"## {title}\n\n{summary}",
                images=[],
                metadata_overrides=[],
                author_id=author,
                source_platform="fluxer",
                source_visible=True,
                status="published",
                is_nsfw=is_nsfw,
                is_abyss=is_abyss,
                visibility=modes.work_visibility(rating, bool(rating_tag.restricts)),
                created_at=now - timedelta(hours=index + 1),
                published_at=now - timedelta(hours=index + 1),
                updated_at=now - timedelta(hours=index + 1),
                last_active_at=now - timedelta(hours=index + 1),
            )
            session.add(work)
            await session.flush()
            for key in [("rating", rating_name), ("type", type_name), *extra]:
                session.add(
                    SiteWorkTag(work_id=work.id, tag_id=tags[key].id, added_by=author)
                )
            if cover_rating:
                cover = Attachment(
                    owner_id=author,
                    purpose="cover",
                    work_id=work.id,
                    kind="image",
                    filename="cover.png",
                    mime="image/png",
                    size=2048,
                    storage_key=f"seed/works/{work.id}/cover.png",
                    state="ready",
                    rating=cover_rating,
                    auto_rating=cover_rating,
                    review_state="auto",
                    width=600,
                    height=800,
                    completed_at=now,
                )
                session.add(cover)
                await session.flush()
                work.cover_attachment_id = cover.id
            if file_name:
                version = WorkVersion(
                    work_id=work.id,
                    name="v1.0",
                    changelog="首个版本",
                    license="仅供示例",
                    dependencies=[],
                    created_by=author,
                    created_at=now - timedelta(hours=index + 1),
                )
                session.add(version)
                image = file_name.endswith(".png")
                file = Attachment(
                    owner_id=author,
                    purpose="version_file",
                    work_id=work.id,
                    kind="image" if image else "file",
                    filename=file_name,
                    mime="image/png" if image else "application/zip",
                    size=4096,
                    storage_key=f"seed/works/{work.id}/{file_name}",
                    state="ready",
                    rating=rating if image else "general",
                    review_state="auto",
                    completed_at=now,
                )
                session.add(file)
                await session.flush()
                session.add(
                    WorkVersionFile(version_id=version.id, attachment_id=file.id)
                )
            await session.execute(
                insert(WorkStatistics)
                .values(work_id=work.id, **stats)
                .on_conflict_do_update(index_elements=[WorkStatistics.work_id], set_=stats)
            )
            print(f"作品 {title} -> {work.id}（{rating}）")
        await session.commit()


async def main() -> None:
    accounts = await seed_accounts(AsyncSessionFactory)
    await seed_tag_catalog(AsyncSessionFactory)
    await seed_works(AsyncSessionFactory, accounts)
    from shared.database import async_engine

    await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

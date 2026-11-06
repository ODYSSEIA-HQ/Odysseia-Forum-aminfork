"""/api/v1 层的种子数据：契约测试账号、tag 目录（内容参考 web 仓库 src/mock/seed.ts）。

用法（库已 alembic upgrade head）：
    DATABASE_URL=postgresql+asyncpg://odysseia:changeme@localhost:5432/odysseia_dev \\
    FLUXER_FAKE=1 uv run python scripts/seed_site_v1.py

- 幂等：重复跑会更新已有行，不会重复插入。
- 账号只在 FLUXER_FAKE=1 时建（真实环境的账号在 Fluxer 里，密码 Forum 不持有）；
  真实环境下只给已存在的同名账号补站内状态（资格、权限、新人期）。
- 已发布作品、版本文件、主榜等等作品/上传/榜单领域做好后在 seed_works() 里补。
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


async def seed_work_version(
    session_factory,
    work_id: int,
    owner_id: str,
    *,
    name: str = "v1.0",
    filename: str = "character-card.json",
    data: bytes | None = None,
    mime: str = "application/json",
) -> tuple[int, int]:
    """给种子作品建「最新版本 + 一个真实文件」（文件落到当前存储，本地是 SITE_STORAGE_LOCAL_DIR）。

    幂等（同名版本已存在就跳过）；返回 (version_id, attachment_id)。契约测试 WRK-15/16 要用。
    实现在 site_v1/services/versions.seed_version_with_file。
    """
    from site_v1.services.versions import seed_version_with_file

    return await seed_version_with_file(
        session_factory,
        work_id=work_id,
        owner_id=owner_id,
        name=name,
        filename=filename,
        data=data,
        mime=mime,
    )


async def seed_works(session_factory, accounts: dict[str, str]) -> None:
    """入口：已发布的全年龄作品（最新版本带文件）、R18 作品（R18 封面）、主榜。

    作品、上传、榜单领域做好后在这里补（契约测试 WRK-15/16、R18-15、05 的榜单用例要用）。
    """
    return None


async def main() -> None:
    accounts = await seed_accounts(AsyncSessionFactory)
    await seed_tag_catalog(AsyncSessionFactory)
    await seed_works(AsyncSessionFactory, accounts)
    from shared.database import async_engine

    await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

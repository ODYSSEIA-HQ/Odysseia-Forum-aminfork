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


def seed_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """纯色 PNG（种子封面、R18 角色卡用）。"""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes(rgb) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


async def seed_works(session_factory, accounts: dict[str, str]) -> None:
    """已发布的全年龄作品（最新版本带文件）、R18 作品（R18 封面）。

    主榜（GET /boards）按 tag 目录生成（全站 + 类型/受众/形态/题材各 tag），不需要单独的数据。
    封面和版本文件都落真实文件到当前存储（本地是 SITE_STORAGE_LOCAL_DIR），下载（WRK-15/16）、
    R18 封面的签名地址（R18-15）才能真的取到内容。
    幂等：按标题找已有作品，存在就只补版本（seed_work_version 本身幂等）。
    """
    from models.attachment import Attachment
    from models.site_tag import SiteWorkTag
    from models.work import Work
    from models.work_statistics import WorkStatistics
    from site_v1 import mode as modes
    from site_v1.services.attachments import storage_key
    from site_v1.storage import get_storage

    author = accounts.get("demo")
    if author is None:
        print("跳过预置作品：没有 demo 账号")
        return
    now = utc_now()
    versions: list[tuple[int, str]] = []
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
                if file_name:
                    versions.append((exists[0], file_name))
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
                data = seed_png(60, 80, (200, 80, 120) if cover_rating != "general" else (90, 140, 200))
                key = storage_key("cover.png")
                await get_storage().put(key, data, "image/png")
                cover = Attachment(
                    owner_id=author,
                    purpose="cover",
                    work_id=work.id,
                    kind="image",
                    filename="cover.png",
                    mime="image/png",
                    size=len(data),
                    storage_key=key,
                    state="ready",
                    rating=cover_rating,
                    auto_rating=cover_rating,
                    review_state="auto",
                    width=60,
                    height=80,
                    completed_at=now,
                )
                session.add(cover)
                await session.flush()
                work.cover_attachment_id = cover.id
            if file_name:
                versions.append((work.id, file_name))
            await session.execute(
                insert(WorkStatistics)
                .values(work_id=work.id, **stats)
                .on_conflict_do_update(index_elements=[WorkStatistics.work_id], set_=stats)
            )
            print(f"作品 {title} -> {work.id}（{rating}）")
        await session.commit()

    # 最新版本 + 真实文件（上传领域的 seed_work_version，幂等）
    for work_id, file_name in versions:
        if file_name.endswith(".png"):
            data, mime = seed_png(40, 40, (180, 60, 90)), "image/png"
        else:
            import io
            import zipfile

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("theme.json", '{"name": "种子美化"}')
            data, mime = buf.getvalue(), "application/zip"
        version_id, file_id = await seed_work_version(
            session_factory, work_id, author, filename=file_name, data=data, mime=mime
        )
        print(f"作品 {work_id} 版本 {version_id}，文件 {file_id}（{file_name}）")


# ---------------- 活动（3.22）、娱乐区（/fun）：events 块 ----------------


async def seed_events(session_factory, accounts: dict[str, str]) -> None:
    """示例活动（参考 web 仓库 src/mock/activities.ts 的 seedEvents）。按 slug 幂等。

    收稿中（demo 还能再交一件）、已结束有结果（混合分级按组）、评定中（demo 是主办人）、
    即将开始、只对 R18 模式可见、草稿。参赛作品用 seed_works 的预置作品（作者都是 demo）。
    """
    from models.attachment import Attachment
    from models.site_event import SiteEvent, SiteEventEntry, SiteEventLog
    from models.work import Work
    from site_v1.services.attachments import storage_key
    from site_v1.storage import get_storage

    demo, lin = accounts.get("demo"), accounts.get("lin")
    if demo is None:
        print("跳过示例活动：没有 demo 账号")
        return
    lin = lin or demo
    now = utc_now()

    def at(days: float):
        return now + timedelta(days=days)

    async with session_factory() as session:
        works = {
            w.title: w
            for w in (
                await session.execute(select(Work).where(Work.deleted_at.is_(None), Work.title.like("【示例】%")))
            ).scalars()
        }

        async def cover(title: str, rgb) -> int:
            data = seed_png(120, 52, rgb)
            key = storage_key("event-cover.png")
            await get_storage().put(key, data, "image/png")
            att = Attachment(
                owner_id=demo, purpose="cover", kind="image", filename="event-cover.png", mime="image/png",
                size=len(data), storage_key=key, state="ready", rating="general", auto_rating="general",
                review_state="auto", width=120, height=52, completed_at=now,
            )
            session.add(att)
            await session.flush()
            return att.id

        general = {"resourceTypes": [], "requiredTags": [], "ratings": ["general"], "newWorksSince": None,
                   "maxEntriesPerUser": 3, "allowNewcomers": True}
        specs = [
            dict(slug="autumn-2026", title="秋日物语 · 主题创作月", rgb=(214, 140, 60),
                 summary="以「秋天」为主题创作角色卡、世界书或美化，截止前随时可以提交。",
                 body="## 活动介绍\n\n秋天到了，来写一张关于秋天的卡吧！\n\n- 主题：秋天、丰收、落叶、告别……都可以\n- 资源类型不限\n- 截止后由主办人评定，公布获奖作品",
                 rules="1. 只能提交自己的作品。\n2. 截止后锁定当时的最新版本参评，之后照常可以更新。\n3. 主办人可以拒绝不符合主题的作品，并写明理由。",
                 start=-6, end=8, result=15, eligibility=general, organizers=[lin],
                 divisions=[{"id": "ev_autumn_d1", "name": "全年龄组", "ratings": ["general"]}],
                 awards=[{"id": "ev_autumn_a1", "divisionId": "ev_autumn_d1", "name": "金叶奖", "description": "最佳作品", "order": 1, "decorRoleId": None},
                         {"id": "ev_autumn_a2", "divisionId": "ev_autumn_d1", "name": "银叶奖", "description": "优秀作品", "order": 2, "decorRoleId": None}],
                 entries=[("【示例】角色卡", "ev_autumn_d1", {}), ("【示例】世界书", "ev_autumn_d1", {})]),
            dict(slug="summer-cards-2026", title="夏日角色卡大赛", rgb=(60, 150, 210),
                 summary="2026 年夏季角色卡比赛，分全年龄组和 R18 组评奖。",
                 body="## 夏日角色卡大赛\n\n收稿期已经结束，结果见「结果」标签。", rules="1. 同时收全年龄和 R18 作品，按分级分组评奖。",
                 start=-60, end=-30, result=-20, published=-20,
                 eligibility={**general, "ratings": ["general", "r18"], "maxEntriesPerUser": 2}, organizers=[lin],
                 divisions=[{"id": "ev_summer_general", "name": "全年龄组", "ratings": ["general"]},
                            {"id": "ev_summer_r18", "name": "R18 组", "ratings": ["r18"]}],
                 awards=[{"id": "ev_summer_g1", "divisionId": "ev_summer_general", "name": "一等奖", "description": "全年龄组最佳", "order": 1, "decorRoleId": None},
                         {"id": "ev_summer_r1", "divisionId": "ev_summer_r18", "name": "一等奖", "description": "R18 组最佳", "order": 1, "decorRoleId": None}],
                 entries=[("【示例】角色卡", "ev_summer_general", {"award_id": "ev_summer_g1", "award_note": "人设完整，开场白很有画面感。"}),
                          ("【示例】R18 角色卡", "ev_summer_r18", {"award_id": "ev_summer_r1", "award_note": "剧情张力强。"})]),
            dict(slug="tools-2026", title="工具与美化创意赛", rgb=(120, 90, 200),
                 summary="给酒馆做点好用的东西：美化、插件、工具、预设都可以。",
                 body="## 工具与美化创意赛\n\n收稿已经截止，主办人正在评定中。", rules="1. 收美化、插件、工具、预设。\n2. 截止时锁定参赛版本。",
                 start=-25, end=-2, result=5, eligibility={**general, "allowNewcomers": False}, organizers=[demo],
                 divisions=[{"id": "ev_tools_d1", "name": "全年龄组", "ratings": ["general"]}],
                 awards=[{"id": "ev_tools_a1", "divisionId": "ev_tools_d1", "name": "最佳创意", "description": "", "order": 1, "decorRoleId": None}],
                 entries=[("【示例】全年龄美化包", "ev_tools_d1", {}), ("【示例】插件", "ev_tools_d1", {}),
                          ("【示例】工具", "ev_tools_d1", {}),
                          ("【示例】预设", "ev_tools_d1", {"state": "rejected", "state_reason": "作品和活动主题无关"})]),
            dict(slug="winter-2026", title="冬至世界书共创", rgb=(150, 190, 220),
                 summary="冬至前后开放收稿，围绕「冬」写世界书。", body="## 冬至世界书共创\n\n还没开始收稿，可以先关注。",
                 rules="1. 只收世界书。\n2. 只收活动开始后发布的新作。", start=12, end=40, result=50,
                 eligibility={**general, "maxEntriesPerUser": 1}, organizers=[lin],
                 divisions=[{"id": "ev_winter_d1", "name": "全年龄组", "ratings": ["general"]}],
                 awards=[{"id": "ev_winter_a1", "divisionId": "ev_winter_d1", "name": "最佳世界书", "description": "", "order": 1, "decorRoleId": None}],
                 entries=[]),
            dict(slug="night-2026", title="深夜限定征集", rgb=(70, 40, 90), audience="r18",
                 summary="只收 R18 角色卡的征集活动。", body="## 深夜限定征集\n\n只对开启 R18 模式的账号可见。", rules="1. 只收 R18 角色卡。",
                 start=-4, end=6, result=12, eligibility={**general, "ratings": ["r18"], "maxEntriesPerUser": 1, "allowNewcomers": False},
                 organizers=[lin], divisions=[{"id": "ev_night_d1", "name": "R18 组", "ratings": ["r18"]}],
                 awards=[{"id": "ev_night_a1", "divisionId": "ev_night_d1", "name": "深夜之星", "description": "", "order": 1, "decorRoleId": None}],
                 entries=[("【示例】R18 角色卡", "ev_night_d1", {})]),
            dict(slug="spring-2027", title="新春贺岁活动（筹备中）", rgb=(200, 60, 60), manual="draft",
                 summary="筹备中的活动，还没发布。", body="草稿", rules="", start=90, end=110, result=120,
                 eligibility=general, organizers=[], divisions=[{"id": "ev_draft_d1", "name": "全年龄组", "ratings": ["general"]}],
                 awards=[], entries=[]),
        ]
        for spec in specs:
            exists = (await session.execute(select(SiteEvent.id).where(SiteEvent.slug == spec["slug"]))).first()
            if exists:
                print(f"活动 {spec['slug']} 已存在（{exists[0]}），跳过")
                continue
            e = SiteEvent(
                slug=spec["slug"], title=spec["title"], summary=spec["summary"], body=spec["body"], rules=spec["rules"],
                cover_attachment_id=await cover(spec["title"], spec["rgb"]), audience=spec.get("audience", "all"),
                manual_state=spec.get("manual", "published"), submit_start_at=at(spec["start"]),
                submit_end_at=at(spec["end"]), result_at=at(spec["result"]), eligibility=spec["eligibility"],
                divisions=spec["divisions"], awards=spec["awards"], organizer_ids=spec["organizers"], results_draft=[],
                results_published_at=at(spec["published"]) if "published" in spec else None,
                created_at=at(min(spec["start"], 0) - 14), updated_at=at(min(spec["end"], 0)),
            )
            session.add(e)
            await session.flush()
            draft: dict[str, list[str]] = {}
            notes: dict[str, dict[str, str]] = {}
            for title, division, extra in spec["entries"]:
                w = works.get(title)
                if w is None:
                    continue
                en = SiteEventEntry(
                    event_id=e.id, division_id=division, work_id=w.id, submitted_by=w.author_id,
                    note=f"「{w.title}」参加{e.title}。", state=extra.get("state", "accepted"),
                    state_reason=extra.get("state_reason"), award_id=extra.get("award_id"),
                    award_note=extra.get("award_note", ""), submitted_at=max(e.submit_start_at, w.created_at) + timedelta(hours=1),
                )
                session.add(en)
                await session.flush()
                if en.award_id:
                    draft.setdefault(en.award_id, []).append(str(en.id))
                    notes.setdefault(en.award_id, {})[str(en.id)] = en.award_note
            e.results_draft = [{"awardId": a, "entryIds": ids, "notes": notes[a]} for a, ids in draft.items()]
            session.add(SiteEventLog(event_id=e.id, actor_id=demo, action="create", detail="建立活动（草稿）", at=e.created_at))
            if e.manual_state == "published":
                session.add(SiteEventLog(event_id=e.id, actor_id=demo, action="publish", detail="发布活动", at=e.created_at + timedelta(hours=1)))
            if e.results_published_at:
                session.add(SiteEventLog(event_id=e.id, actor_id=demo, action="results_publish", detail="公布结果", at=e.results_published_at))
            print(f"活动 {spec['slug']} -> {e.id}")
        await session.commit()


async def seed_fun(session_factory, accounts: dict[str, str]) -> None:
    """娱乐区：几个账号先有点类脑币（排行榜演示），股市开张并预跑 60 天历史。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from models.site_fun import SiteFunWallet
    from site_v1.services.fun import stocks

    balances = {"lin": 2650, "newbie": 860}
    now = utc_now()
    async with session_factory() as session:
        for username, balance in balances.items():
            user_id = accounts.get(username)
            if user_id:
                await session.execute(
                    pg_insert(SiteFunWallet)
                    .values(user_id=user_id, balance=balance, created_at=now, updated_at=now)
                    .on_conflict_do_nothing(index_elements=[SiteFunWallet.user_id])
                )
        await stocks.ensure_market(session)
        await session.commit()
    print("娱乐区：钱包、股市已就绪")


async def main() -> None:
    accounts = await seed_accounts(AsyncSessionFactory)
    await seed_tag_catalog(AsyncSessionFactory)
    await seed_works(AsyncSessionFactory, accounts)
    await seed_events(AsyncSessionFactory, accounts)
    await seed_fun(AsyncSessionFactory, accounts)
    from shared.database import async_engine

    await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

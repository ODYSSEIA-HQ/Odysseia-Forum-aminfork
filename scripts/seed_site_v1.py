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
                data = seed_png(
                    60,
                    80,
                    (200, 80, 120) if cover_rating != "general" else (90, 140, 200),
                )
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
                .on_conflict_do_update(
                    index_elements=[WorkStatistics.work_id], set_=stats
                )
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


# ---------------- 「我的」、公告规则、作品内、好友、勋章（me 块） ----------------

COMMUNITY_RULES = """## 一、基本原则

1. 友善交流，不人身攻击、不引战、不刷屏。
2. 尊重原创：转载、二创请注明出处，作者明确不允许的不要搬运。
3. 不发布违法内容和侵犯他人隐私的内容。

## 二、发布作品

- 分级 tag 必须如实选择。**R18 内容只对开启了 R18 模式的账号可见**，全年龄模式下看不到、也看不出它存在。
- 封面和图库图片会经过自动分级；图片分级高于作品分级时，需要换图或提高作品分级。
- 作品说明里写清楚适用的模型和使用方法，方便大家反馈。

## 三、交流区

- 交流区是作品的主体，作者和卡贴管理员可以删言、在本作品禁言，被处理的人可以申诉。
- @全体 只有作者和管理员能用，请不要滥用。

## 四、处罚

| 情况 | 处理 |
|---|---|
| 首次轻微违规 | 提醒 |
| 多次违规 | 本作品或全站禁言 |
| 严重违规 | 封禁账号 |
"""

R18_RULES = """## R18 内容规则

- 只有满 18 岁、确认了条款、并在设置里打开 R18 模式的账号才能看到 R18 内容。
- R18G 默认不显示，需要在「内容」设置里单独打开。
- 交流区里发的 R18 图片只能出现在受限作品的交流区。
- 发现分级不实，可以在作品页举报，或在下载后帮忙标记。
"""

IMAGE_POLICY = """## 图片保留政策

- 上传的图片原样保存，角色卡 PNG 不会被重新编码。
- 被自动分级拦截的图片保留 30 天供申诉，之后删除。
- 删除作品后，图片在 7 天后从存储里清除。
"""

FAQ = """## 常见问题

**为什么我看不到某些作品？**
可能是 R18 作品，而你现在在全年龄模式；或者作品带了你屏蔽的 tag、作者被你屏蔽了。

**怎么认领我在 DC 发过的作品？**
在「设置 → 账号」里绑定 Discord，迁移过来的作品会自动转到你的账号下。

**私信收不到？**
对方可能设置了只接收好友的私信，先加好友试试。
"""

RULE_DOCS = [
    ("community", "社区规则", "发布、交流和处罚的基本约定。", COMMUNITY_RULES, 1),
    ("r18", "R18 内容规则", "谁能看到 R18 内容、R18G 和图片的规则。", R18_RULES, 1),
    ("images", "图片保留政策", "图片怎么存、被拦截的图片留多久。", IMAGE_POLICY, 1),
    ("faq", "常见问题", "看不到作品、认领 DC 作品、收不到私信……", FAQ, 2),
]

# (标题, audience, kind, requiresAck, 置顶, ruleSlug, 几天前, 正文)
SITE_ANNOUNCEMENTS = [
    (
        "社区规则（2026-10 版）",
        "all",
        "rule",
        True,
        True,
        "community",
        20,
        "欢迎来到类脑资源平台。发布作品请如实打分级 tag，R18 内容只对开启 R18 模式的账号可见。",
    ),
    (
        "新平台测试中",
        "all",
        "banner",
        False,
        False,
        None,
        15,
        "这是新前端的预览站，数据都是示例。",
    ),
    (
        "私信和好友上线",
        "all",
        "normal",
        False,
        False,
        None,
        1,
        "现在可以给其他用户发私信了。不做群组，只有一对一私信；可以在「设置 → 隐私 → 谁可以给我发私信」里改成「只有好友」。",
    ),
    (
        "本周六凌晨维护",
        "all",
        "normal",
        False,
        False,
        None,
        2,
        "周六 02:00–04:00 维护数据库，期间不能发布作品和发言，浏览不受影响。",
    ),
    (
        "R18 内容规则调整",
        "r18",
        "rule",
        True,
        False,
        "r18",
        4,
        "R18G 改为默认不显示，需要在「内容」设置里单独打开。交流区里的 R18 图片只能发在受限作品的交流区。",
    ),
    (
        "索引页功能迁移进度",
        "all",
        "normal",
        False,
        False,
        None,
        6,
        "收藏、关注、作者页、提醒、私信已经迁到新平台。书单、抽卡、活动正在迁移。",
    ),
]

DECORATION_ROLES = [
    (
        "deco_founder",
        "元老",
        "平台内测期就在的老朋友",
        "#d4a017",
        "legendary",
        ["nameColor", "frame"],
        30,
    ),
    (
        "deco_event_2026",
        "十月活动获奖",
        "活动：十月角色卡大赛 获奖",
        "#7c5cff",
        "epic",
        ["frame"],
        20,
    ),
    ("deco_helper", "热心答疑", "在交流区帮助了很多人", "#2e9d6a", "rare", [], 10),
]


async def seed_me_block(session_factory, accounts: dict[str, str]) -> None:
    """规则中心、站内公告、勋章、示例好友申请、作品规则/公告/钉选（只在 FLUXER_FAKE=1 的开发/测试环境）。

    幂等：规则按 slug、公告按标题、勋章按 ID，已有的不重复建。
    """
    from models.site_me import (
        SiteAnnouncement,
        SiteDecorationRole,
        SiteFriendRequest,
        SiteRuleDoc,
        SiteUserDecorationGrant,
        SiteWorkPin,
        SiteWorkRules,
    )
    from models.work import Work
    from models.work_announcement import WorkAnnouncement

    if not get_config().fluxer_fake:
        print("跳过公告、规则等示例数据：只在 FLUXER_FAKE=1 时建")
        return
    now = utc_now()
    async with session_factory() as session:
        for i, (slug, title, summary, body, version) in enumerate(RULE_DOCS):
            await session.execute(
                insert(SiteRuleDoc)
                .values(
                    slug=slug,
                    title=title,
                    summary=summary,
                    body=body,
                    version=version,
                    sort=i,
                    updated_at=now - timedelta(days=10 - i),
                )
                .on_conflict_do_nothing()
            )
        existing = set(
            (await session.execute(select(SiteAnnouncement.title))).scalars().all()
        )
        for title, audience, kind, ack, pinned, slug, days, body in SITE_ANNOUNCEMENTS:
            if title in existing:
                continue
            session.add(
                SiteAnnouncement(
                    title=title,
                    audience=audience,
                    kind=kind,
                    requires_ack=ack,
                    pinned=pinned,
                    rule_slug=slug,
                    body=body,
                    version=1,
                    published_at=now - timedelta(days=days),
                )
            )
        for rid, name, desc, color, rarity, slots, sort in DECORATION_ROLES:
            await session.execute(
                insert(SiteDecorationRole)
                .values(
                    id=rid,
                    name=name,
                    description=desc,
                    color=color,
                    rarity=rarity,
                    slots=slots,
                    sort=sort,
                )
                .on_conflict_do_nothing()
            )
        demo, lin = accounts.get("demo"), accounts.get("lin")
        if demo:
            for rid, *_ in DECORATION_ROLES:
                await session.execute(
                    insert(SiteUserDecorationGrant)
                    .values(
                        user_id=demo, role_id=rid, granted_at=now, source="示例数据"
                    )
                    .on_conflict_do_nothing()
                )
        if demo and lin:
            await session.execute(
                insert(SiteFriendRequest)
                .values(from_id=lin, to_id=demo, created_at=now)
                .on_conflict_do_nothing()
            )
        # 第一个预置作品：交流区规则、一条公告、两条钉选
        work = (
            (
                await session.execute(
                    select(Work)
                    .where(Work.author_id == demo, Work.deleted_at.is_(None))
                    .order_by(Work.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if demo
            else None
        )
        if work is not None:
            await session.execute(
                insert(SiteWorkRules)
                .values(
                    work_id=work.id,
                    version=1,
                    updated_at=work.created_at,
                    body="1. 友善交流，不引战。\n2. 反馈问题请附上使用的模型和设置。\n3. 二创请注明出处。",
                )
                .on_conflict_do_nothing()
            )
            has_ann = (
                await session.execute(
                    select(WorkAnnouncement.id).where(
                        WorkAnnouncement.work_id == work.id
                    )
                )
            ).first()
            if not has_ann:
                session.add(
                    WorkAnnouncement(
                        work_id=work.id,
                        author_id=demo,
                        title="更新预告",
                        body="下个版本会加入新的开场白，欢迎在交流区提建议。",
                        created_at=work.created_at,
                    )
                )
            has_pin = (
                await session.execute(
                    select(SiteWorkPin.id).where(SiteWorkPin.work_id == work.id)
                )
            ).first()
            if not has_pin:
                session.add(
                    SiteWorkPin(
                        work_id=work.id,
                        author_id=demo,
                        pinned_by=demo,
                        excerpt="常见问题：开场白不触发时，先检查世界书是否已启用，再看模型上下文是否够长。",
                    )
                )
                session.add(
                    SiteWorkPin(
                        work_id=work.id,
                        author_id=lin,
                        pinned_by=demo,
                        excerpt="推荐设置：温度 0.9，上下文 16k 以上效果最好。",
                    )
                )
        await session.commit()
    print("公告、规则、勋章、示例好友申请、作品规则/公告/钉选已就绪")


async def main() -> None:
    accounts = await seed_accounts(AsyncSessionFactory)
    await seed_tag_catalog(AsyncSessionFactory)
    await seed_works(AsyncSessionFactory, accounts)
    await seed_me_block(AsyncSessionFactory, accounts)
    from shared.database import async_engine

    await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

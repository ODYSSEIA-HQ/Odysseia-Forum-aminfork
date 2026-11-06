"""旧标签迁成协议 tag（协议 7.3，scripts/migrate_legacy_tags.py）。

造一份接近真实的旧数据：DC 原生标签（tag.source=discord 经 tag_binding 同步、source=fluxer 经
work_native_tag_binding）、索引页 7 类自定义标签（带赞数）、深渊向标签、已结束的绑定、书单上的绑定、
已删除的作品、作品上已有的协议 tag、三种用户的屏蔽偏好；跑 dry-run、正式迁移、重跑。
"""

import importlib.util
import os
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from models.discord_identity import DiscordIdentity
from models.discord_tag_source import DiscordTagSource
from models.site_account import SiteUserPrefs
from models.site_tag import SiteTag, SiteWorkTag
from models.site_tag_migration import SiteTagMigrationRecord
from models.tag import Tag
from models.tag_binding import TagBinding
from models.tag_vote import TagVote
from models.thread import Thread
from models.user import User
from models.user_search_preferences import UserSearchPreferences
from models.work import Work
from models.work_native_tag_binding import WorkNativeTagBinding
from models.work_native_tag_source import WorkNativeTagSource
from shared.time_utils import utc_now
from site_v1.legacy_tag_migration import Mapping, clean_name, migrate, norm_key, write_json

_SEED = os.path.join(os.path.dirname(__file__), "../../scripts/seed_site_v1.py")


def _seed_module():
    spec = importlib.util.spec_from_file_location("seed_site_v1", _SEED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_clean_name_and_keys():
    assert clean_name("长 篇 / 剧情") == "长篇·剧情"
    assert clean_name("🌸校园🌸") == "校园"
    assert clean_name("ＡＢＣ（全角）") == "ABC全角"
    assert clean_name("黑化/病娇、恶堕") == "黑化·病娇·恶堕"
    assert clean_name("!!!") == ""
    assert clean_name("一" * 30) == "一" * 20
    assert clean_name("R-18G 向") == "R·18G向"
    assert norm_key(" Discord  BOT ") == norm_key("discord bot")
    m = Mapping.load()
    assert m.entries[norm_key("群像/多角色/后宫")] == [("theme", "后宫"), ("attribute", "多角色")]
    assert m.entries[norm_key("其他")] == [("drop", None)]
    assert (m.pending_group, m.pending_name, m.max_tags) == ("theme", "待分类", 15)


async def _legacy_data(factory):
    """返回造出来的各种 ID。"""
    now = utc_now()
    ids = {}
    async with factory() as s:
        s.add_all(
            [
                User(id="fx_author", username="author"),
                User(id="fx_reader", username="reader"),
                User(id="fx_other", username="other"),
            ]
        )
        s.add(DiscordIdentity(discord_user_id="555000111", user_id="fx_reader", version=1))

        def work(title, **kw):
            w = Work(title=title, author_id="fx_author", status="published", **kw)
            s.add(w)
            return w

        w1, w2, w3, w4, w5, w6 = (
            work("综合标签作品"),
            work("超额标签作品"),
            work("对不上的作品"),
            work("target_type=work 的作品"),
            work("已删除作品", deleted_at=now),
            work("Fluxer 原生标签作品"),
        )
        await s.flush()
        ids.update(w1=w1.id, w2=w2.id, w3=w3.id, w4=w4.id, w5=w5.id, w6=w6.id)
        # 每个作品对应一个 DC 主帖（tag_binding 挂在帖子上，work_id 由帖子得出）
        threads = {}
        for i, w in enumerate((w1, w2, w3, w4, w5, w6)):
            th = Thread(
                work_id=w.id, guild_id=1001, channel_id=2002, thread_id=7700000 + i,
                title=w.title, author_id=111, role="main",
            )
            s.add(th)
            threads[w.id] = th
        await s.flush()

        tags = {}

        def tag(key, name, source="discord", category=None, **kw):
            t = Tag(name=name, source=source, category=category, **kw)
            s.add(t)
            tags[key] = t
            return t

        # DC 原生（discord 同步）
        tag("纯爱", "纯爱")
        tag("群像", "群像/多角色/后宫")
        tag("支配", "支配/调教/束缚/SM")
        tag("病娇", "病娇/黑化/恶堕")
        tag("其他", "其他")
        tag("百合", "百合")
        tag("人外", "人外")
        tag("古风", "古风")
        tag("现代", "现代")
        tag("幻想", "幻想")
        tag("纯文字", "纯文字")
        tag("伪娘", "伪娘/男娘")
        # DC 转过来还没分类的概念（custom、无分类）
        tag("前端美化", "前端/美化", source="custom", originated_from_discord=True)
        # Fluxer 原生
        tag("fx_ntr", "NTR", source="fluxer")
        tag("fx_scp", "数据删除（SCP、后室等）", source="fluxer")
        # 自定义 7 类
        tag("猫娘", "猫娘", "custom", 1)
        tag("黑化c", "黑化", "custom", 3)  # 和「病娇/黑化/恶堕」拆出来的「黑化」撞名
        tag("剧情", "长 篇 / 剧情", "custom", 5)
        tag("长名", "一个名字特别特别长的自定义标签超过二十个字了吧", "custom", 4)
        tag("校园", "🌸校园🌸", "custom", 6)
        tag("纯爱c", "纯爱", "custom", 2)  # 和官方「纯爱」同名
        tag("深渊", "肢体破坏", "custom", 1, is_abyss=True)
        tag("空名", "！！！", "custom", 7)
        for i in range(14):
            tag(f"多{i}", f"玩法{i:02d}", "custom", 7)
        tag("已删", "已删除标签", "custom", 1, deleted_at=now)
        tag("书单用", "书单标签", "custom", 2)
        await s.flush()
        ids["tags"] = {k: t.id for k, t in tags.items()}
        for key, t in tags.items():
            if t.source == "discord":
                s.add(DiscordTagSource(discord_tag_id=6600000 + t.id, channel_id=2002, tag_id=t.id, name=t.name))
        await s.flush()
        sources = {
            src.tag_id: src for src in (await s.execute(select(DiscordTagSource))).scalars()
        }

        def dc_source(key):
            # DC 同步绑定要有来源记录（真实库里触发器据此投影到 work_native_tag_binding）
            return sources[tags[key].id]

        def bind(work_id, key, *, upvotes=0, age_days=0, native=False, ended=False, target="thread"):
            s.add(
                TagBinding(
                    work_id=work_id if target == "thread" else None,
                    target_type=target,
                    target_id=work_id if target == "work" else threads[work_id].id,
                    tag_id=tags[key].id,
                    binding_source="discord_sync" if native else "local",
                    discord_source_id=dc_source(key).id if native else None,
                    upvotes=upvotes,
                    created_at=now - timedelta(days=age_days),
                    ended_at=now if ended else None,
                )
            )

        # w1：原生（按 DC 顺序）+ 自定义
        for k in ("纯爱", "群像", "支配", "病娇", "其他"):
            bind(w1.id, k, native=True)
        bind(w1.id, "猫娘", upvotes=5)
        bind(w1.id, "黑化c", upvotes=2)
        bind(w1.id, "剧情", upvotes=1)
        bind(w1.id, "长名")
        bind(w1.id, "校园")
        bind(w1.id, "纯爱c", upvotes=3)
        bind(w1.id, "深渊", ended=True)  # 已结束的轮次不迁
        bind(w1.id, "已删")  # 旧标签已删除
        # w2：5 个题材 + 14 个自定义 + 已有协议 tag → 题材留 3 个，总数 15
        for k in ("纯爱", "百合", "人外", "古风", "现代", "纯文字"):
            bind(w2.id, k, native=True)
        for i in range(14):
            bind(w2.id, f"多{i}", upvotes=i % 5, age_days=i)
        # w3：对不上的原生标签 + DC 转换概念 + 深渊向自定义
        bind(w3.id, "伪娘", native=True)
        bind(w3.id, "前端美化")
        bind(w3.id, "深渊", upvotes=9)
        bind(w3.id, "空名")
        # w4：target_type=work
        bind(w4.id, "猫娘", target="work")
        # w5：已删除作品
        bind(w5.id, "纯爱", native=True)
        # 书单上的绑定不算
        s.add(TagBinding(target_type="booklist", target_id=1, tag_id=tags["书单用"].id))
        # w6：Fluxer 原生标签
        for k, ext in (("fx_ntr", "1"), ("fx_scp", "2")):
            src = WorkNativeTagSource(
                platform="fluxer",
                guild_id="g1",
                channel_id="c1",
                external_id=ext,
                tag_id=tags[k].id,
                name=tags[k].name,
                emoji={},
            )
            s.add(src)
            await s.flush()
            s.add(WorkNativeTagBinding(work_id=w6.id, source_id=src.id))
        # 赞踩（不迁）
        await s.flush()
        first_binding = (await s.execute(select(TagBinding.id).limit(1))).scalar_one()
        s.add(TagVote(binding_id=first_binding, user_id=1, vote=1))

        # 屏蔽偏好
        s.add_all(
            [
                UserSearchPreferences(
                    user_id=111, guild_id=1, fluxer_user_id="fx_author",
                    exclude_tags=["NTR", "猫娘", "其他", "从没见过的标签"],
                ),
                UserSearchPreferences(user_id=555000111, guild_id=1, exclude_tags=["病娇/黑化/恶堕", "百合"]),
                UserSearchPreferences(user_id=999, guild_id=1, exclude_tags=["纯爱"]),
            ]
        )
        await s.commit()
    return ids


async def _work_tags(factory, work_id):
    async with factory() as s:
        rows = (
            await s.execute(
                select(SiteTag.group_id, SiteTag.name, SiteWorkTag.added_by)
                .join(SiteWorkTag, SiteWorkTag.tag_id == SiteTag.id)
                .where(SiteWorkTag.work_id == work_id, SiteWorkTag.removed_at.is_(None))
            )
        ).all()
    return {(g, n): by for g, n, by in rows}


async def _count(factory, model):
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.mark.asyncio
async def test_legacy_tag_migration(db_session_factory, tmp_path):
    factory = db_session_factory
    await _seed_module().seed_tag_catalog(factory)
    ids = await _legacy_data(factory)
    T = ids["tags"]
    # w2 上作者已经自己加过一个协议自由 tag
    async with factory() as s:
        own = SiteTag(group_id="free", name="作者自己加的")
        s.add(own)
        await s.flush()
        s.add(SiteWorkTag(work_id=ids["w2"], tag_id=own.id, added_by="fx_author"))
        await s.commit()
    tags_before = await _count(factory, SiteTag)
    votes_before = await _count(factory, TagVote)

    # ---- dry-run：库里什么都不变 ----
    dry = await migrate(factory, dry_run=True, run_id="dry")
    assert dry.stats["works.processed"] == 5 and dry.stats["works.tagsAdded"] > 0
    assert await _count(factory, SiteTag) == tags_before
    assert await _count(factory, SiteWorkTag) == 1
    assert await _count(factory, SiteTagMigrationRecord) == 0
    async with factory() as s:
        assert (await s.get(SiteUserPrefs, "fx_author")) is None
    write_json(dry, tmp_path / "dry.json")
    assert (tmp_path / "dry.json").stat().st_size > 1000

    # ---- 正式迁移 ----
    report = await migrate(factory, run_id="run1")
    write_json(report, tmp_path / "run.json")
    s1 = dict(report.stats)
    # dry-run 和正式跑算出的结果一样
    assert {k: v for k, v in dry.stats.items()} == s1

    # w1：原生按对照表归组（一拆多、丢弃「其他」），自定义变自由 tag（清洗、撞名合并）
    w1 = await _work_tags(factory, ids["w1"])
    assert set(w1) == {
        ("theme", "纯爱"),
        ("theme", "后宫"),
        ("attribute", "多角色"),
        ("warning", "支配"),
        ("warning", "调教"),
        ("warning", "SM"),
        ("attribute", "病娇"),
        ("free", "黑化"),
        ("free", "恶堕"),
        ("free", "猫娘"),
        ("free", "长篇·剧情"),
        ("free", "一个名字特别特别长的自定义标签超过二十个字了吧"[:20]),
        ("free", "校园"),
    }
    # 官方 tag 和原生拆出来的自由 tag 算作者加的；自定义来的算系统
    assert w1[("theme", "纯爱")] == "fx_author" and w1[("free", "恶堕")] == "fx_author"
    assert w1[("free", "猫娘")] is None
    # 「黑化」：原生拆分和自定义撞名，只有一个自由 tag
    async with factory() as s:
        n = (
            await s.execute(select(func.count()).where(SiteTag.name == "黑化", SiteTag.deleted_at.is_(None)))
        ).scalar_one()
        assert n == 1

    # w2：题材 5 个只留前 3 个（DC 顺序），总数 15（已有 1 个），丢掉赞少的自定义
    w2 = await _work_tags(factory, ids["w2"])
    assert len(w2) == 15 and ("free", "作者自己加的") in w2
    assert {n for g, n in w2 if g == "theme"} == {"纯爱", "百合", "人外"}
    assert ("form", "纯文字") in w2
    w2rec = next(w for w in report.works if w["workId"] == str(ids["w2"]))
    assert {d["name"] for d in w2rec["droppedGroupLimit"]} == {"古风", "现代"}
    dropped = w2rec["droppedOverLimit"]
    assert len(dropped) == 4  # 1 已有 + 4 官方 + 10 自由 = 15
    kept_free = [n for g, n in w2 if g == "free" and n != "作者自己加的"]
    assert len(kept_free) == 10
    # 丢的都是赞少的：保留的最小赞数 ≥ 丢掉的最大赞数
    up = {f"玩法{i:02d}": i % 5 for i in range(14)}
    assert min(up[n] for n in kept_free) >= max(d["upvotes"] for d in dropped)

    # w3：对不上 → 原名清洗成自由 tag + 「待分类」；DC 转换概念也按原生走；深渊向自定义变自由 tag；空名丢弃
    w3 = await _work_tags(factory, ids["w3"])
    assert set(w3) == {("free", "伪娘·男娘"), ("free", "前端·美化"), ("theme", "待分类"), ("free", "肢体破坏")}
    # w4：target_type=work 的绑定
    assert set(await _work_tags(factory, ids["w4"])) == {("free", "猫娘")}
    # w5：已删除作品不迁
    assert await _work_tags(factory, ids["w5"]) == {}
    # w6：Fluxer 原生
    assert set(await _work_tags(factory, ids["w6"])) == {("warning", "NTR"), ("theme", "数据删除")}

    # 迁移记录：原分类、动作
    async with factory() as s:
        rec = {
            r.legacy_key: r
            for r in (
                await s.execute(select(SiteTagMigrationRecord).where(SiteTagMigrationRecord.kind == "tag"))
            ).scalars()
        }
    assert rec[str(T["猫娘"])].action == "free" and rec[str(T["猫娘"])].detail["legacyCategory"] == "癖好"
    assert rec[str(T["黑化c"])].action == "merged" and rec[str(T["黑化c"])].detail["legacyCategory"] == "角色"
    assert rec[str(T["纯爱c"])].action == "merged"  # 撞上官方「纯爱」
    assert rec[str(T["深渊"])].detail["isAbyss"] is True and rec[str(T["深渊"])].action == "free"
    assert rec[str(T["其他"])].action == "dropped" and rec[str(T["空名"])].action == "dropped"
    assert rec[str(T["群像"])].action == "official"
    assert rec[str(T["伪娘"])].detail["pending"] is True
    assert str(T["已删"]) not in rec

    # 屏蔽偏好
    async with factory() as s:
        author = await s.get(SiteUserPrefs, "fx_author")
        reader = await s.get(SiteUserPrefs, "fx_reader")
        names = {
            t.id: t.name for t in (await s.execute(select(SiteTag))).scalars()
        }
    assert sorted(names[int(i)] for i in author.blocked_tag_ids) == ["NTR", "猫娘"]
    assert sorted(names[int(i)] for i in reader.blocked_tag_ids) == ["恶堕", "病娇", "百合", "黑化"]
    prefs = {p["prefsId"]: p for p in report.prefs}
    assert sorted(p["action"] for p in prefs.values()) == ["migrated", "migrated", "user_not_found"]
    author_rec = next(p for p in prefs.values() if p["userId"] == "fx_author")
    assert author_rec["missingNames"] == ["其他", "从没见过的标签"]

    # 赞踩不动
    assert await _count(factory, TagVote) == votes_before
    # 统计样例
    assert s1["legacyTags.custom.玩法"] == 15 and s1["prefs.user_not_found"] == 1
    assert s1["works.overLimit"] == 1 and s1["works.pending"] == 1

    # ---- 重跑：幂等 ----
    before_tags = await _count(factory, SiteTag)
    before_bindings = await _count(factory, SiteWorkTag)
    before_records = await _count(factory, SiteTagMigrationRecord)
    again = await migrate(factory, run_id="run2")
    assert again.stats["works.tagsAdded"] == 0
    assert again.stats.get("freeTagsCreated", 0) == 0
    assert again.stats["prefs.unchanged"] == 2
    assert await _count(factory, SiteTag) == before_tags
    assert await _count(factory, SiteWorkTag) == before_bindings
    assert await _count(factory, SiteTagMigrationRecord) == before_records
    async with factory() as s:
        runs = set((await s.execute(select(SiteTagMigrationRecord.run_id))).scalars())
    assert runs == {"run2"}


@pytest.mark.asyncio
async def test_followed_tag_is_not_blocked(db_session_factory):
    factory = db_session_factory
    await _seed_module().seed_tag_catalog(factory)
    async with factory() as s:
        s.add(User(id="fx_u", username="u"))
        s.add(Tag(name="NTR", source="discord"))
        ntr = (
            await s.execute(select(SiteTag).where(SiteTag.group_id == "warning", SiteTag.name == "NTR"))
        ).scalar_one()
        ntr_id = ntr.id
        s.add(SiteUserPrefs(user_id="fx_u", followed_tag_ids=[str(ntr_id)]))
        s.add(UserSearchPreferences(user_id=1, guild_id=1, fluxer_user_id="fx_u", exclude_tags=["NTR"]))
        await s.commit()
    report = await migrate(factory, run_id="f")
    async with factory() as s:
        prefs = await s.get(SiteUserPrefs, "fx_u")
    assert prefs.blocked_tag_ids == []
    assert report.prefs[0]["skippedFollowed"] == [str(ntr_id)]

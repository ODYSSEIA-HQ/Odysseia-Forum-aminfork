"""旧标签迁成协议 tag（协议 7.3）。入口 scripts/migrate_legacy_tags.py，对照表 scripts/legacy_tag_map.yaml。

旧数据（V1/V2 的 tag 体系）：
- tag 表：source=discord/fluxer 是 DC 原生标签；source=custom 且有 category（1–7）是索引页自定义标签；
  source=custom 但没有 category 的是「DC 标签转过来、还没分类」的概念，按原生标签处理。
- 作品上的标签：tag_binding（ended_at 为空，work_id=作品 或 target_type=work）和 work_native_tag_binding
  （→ work_native_tag_source.tag_id）。
- 屏蔽标签：user_search_preferences.exclude_tags（标签名），用户按 fluxer_user_id 或 DC ID（discord_identity）找。

规则：
- 原生标签按对照表归入官方组（可一拆多、可丢弃）；对不上的原名清洗后变自由 tag，作品打「待分类」。
- 自定义标签 7 类全部变自由 tag（group=free），迁移记录里留原分类；深渊向标签也一样。
- 清洗：NFKC；分隔符（/、、，|+&_- 等）换成「·」；去空白和其他符号；最长 20 字。清洗后为空或名字被禁用的丢弃。
- 撞名合并：清洗后和已有 tag（官方、自由、同义词，不分大小写）同名的直接用已有 tag；几个旧标签清洗成同一个名字的合并成一个。
- 每个作品最多 max_tags（15）个：已有的协议 tag 不动、先占名额；再加官方 tag（按 DC 原顺序），
  再加自由 tag（按原赞数从高到低，一样的按添加时间）；组有 maxSelect（题材 3 个、形态 1 个）的超出部分丢弃；
  丢掉的写进迁移记录。官方 tag 和原生标签来的自由 tag 算作者加的（added_by=作者），自定义标签来的算系统加的（空）。
- 屏蔽标签：按同一张映射换成新 tag ID，并进 prefs.blockedTags（已关注的不屏蔽，记下来）。「待分类」不进屏蔽。
- 不迁赞踩、提议、包含/互斥关系、操作记录。

幂等：只增不删（已有绑定不重复加），迁移记录按 (kind, legacy_key) 覆盖。dry_run 在同一事务里算完回滚。
"""

import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from models.discord_identity import DiscordIdentity
from models.site_account import SiteUserPrefs
from models.site_nav import SiteTagAlias, SiteTagDisabled
from models.site_tag import SiteTag, SiteTagGroup, SiteWorkTag
from models.site_tag_migration import SiteTagMigrationRecord
from models.tag import Tag
from models.tag_binding import TagBinding
from models.thread import Thread
from models.user import User
from models.user_search_preferences import UserSearchPreferences
from models.work import Work
from models.work_native_tag_binding import WorkNativeTagBinding
from models.work_native_tag_source import WorkNativeTagSource
from shared.time_utils import utc_now

FREE = "free"
MAX_NAME = 20
CATEGORY_NAMES = {1: "癖好", 2: "作品", 3: "角色", 4: "特质", 5: "情节", 6: "背景", 7: "玩法"}
DEFAULT_MAP = Path(__file__).resolve().parents[2] / "scripts" / "legacy_tag_map.yaml"

_SEPARATORS = re.compile(r"[/／\\|｜、,，;；&＆+＋_\-－~～・•･]+")
_DOTS = re.compile(r"·{2,}")


def norm_key(name: str) -> str:
    """对照表查找用：NFKC、去首尾空白、压缩中间空白、不分大小写。"""
    text = unicodedata.normalize("NFKC", name or "").strip()
    return re.sub(r"\s+", " ", text).casefold()


def clean_name(name: str) -> str:
    """自由 tag 名清洗（规则同 tag_rules.validate_free_tag_name：不能有空格和特殊符号，最长 20 字）。"""
    text = unicodedata.normalize("NFKC", name or "")
    text = _SEPARATORS.sub("·", text)
    text = "".join(ch for ch in text if ch.isalnum() or ch == "·")
    text = _DOTS.sub("·", text).strip("·")
    return text[:MAX_NAME].rstrip("·")


# ---------------- 对照表 ----------------


@dataclass
class Mapping:
    entries: dict[str, list[tuple[str, str | None]]]
    """norm_key(原名) → [(组, 名字)]；("drop", None) 表示丢弃。"""
    unmatched_as_free: bool = True
    mark_pending: bool = True
    pending_group: str = "theme"
    pending_name: str = "待分类"
    max_tags: int = 15

    @classmethod
    def load(cls, path: Path | str = DEFAULT_MAP) -> "Mapping":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        entries: dict[str, list[tuple[str, str | None]]] = {}
        for name, targets in (raw.get("map") or {}).items():
            items = targets if isinstance(targets, list) else [targets]
            parsed = []
            for t in items:
                t = str(t).strip()
                if t == "drop":
                    parsed.append(("drop", None))
                    continue
                group, _, tag_name = t.partition("/")
                if not tag_name:
                    raise ValueError(f"对照表 {name!r} 的目标 {t!r} 要写成「组/名字」")
                parsed.append((group.strip(), tag_name.strip()))
            entries[norm_key(str(name))] = parsed
        unmatched = raw.get("unmatched") or {}
        pending = raw.get("pending_tag") or {}
        return cls(
            entries=entries,
            unmatched_as_free=bool(unmatched.get("as_free", True)),
            mark_pending=bool(unmatched.get("mark_pending", True)),
            pending_group=str(pending.get("group", "theme")),
            pending_name=str(pending.get("name", "待分类")),
            max_tags=int(raw.get("max_tags", 15)),
        )


# ---------------- 结果 ----------------


@dataclass
class Target:
    tag_id: int
    name: str
    group: str
    official: bool


@dataclass
class Resolution:
    """一个旧标签迁成什么。"""

    legacy_id: int | None
    legacy_name: str
    source: str
    native: bool
    category: int | None = None
    is_abyss: bool = False
    targets: list[Target] = field(default_factory=list)
    action: str = "free"
    pending: bool = False
    """原生标签对不上：作品要打「待分类」。"""
    cleaned: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def detail(self) -> dict:
        return {
            "legacyName": self.legacy_name,
            "source": self.source,
            "native": self.native,
            "legacyCategory": CATEGORY_NAMES.get(self.category) if self.category else None,
            "legacyCategoryId": self.category,
            "isAbyss": self.is_abyss,
            "cleanedNames": self.cleaned,
            "pending": self.pending,
            "newTags": [
                {"id": str(t.tag_id), "name": t.name, "group": t.group} for t in self.targets
            ],
            "notes": self.notes,
        }


@dataclass
class Report:
    run_id: str
    dry_run: bool
    stats: dict = field(default_factory=lambda: defaultdict(int))
    tags: list[dict] = field(default_factory=list)
    works: list[dict] = field(default_factory=list)
    prefs: list[dict] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "runId": self.run_id,
            "dryRun": self.dry_run,
            "stats": dict(sorted(self.stats.items())),
            "tags": self.tags,
            "works": self.works,
            "prefs": self.prefs,
        }


# ---------------- 迁移 ----------------


class _Catalog:
    """协议 tag 目录的内存索引；按需建自由 tag（同一事务内）。"""

    def __init__(self, session, report: Report):
        self.session = session
        self.report = report
        self.official: dict[tuple[str, str], SiteTag] = {}
        self.by_lower: dict[str, SiteTag] = {}
        self.alias: dict[str, int] = {}
        self.disabled: set[str] = set()
        self.by_id: dict[int, SiteTag] = {}
        self.max_select: dict[str, int] = {}
        self.created_free: set[int] = set()

    async def load(self):
        for tag in (
            await self.session.execute(select(SiteTag).where(SiteTag.deleted_at.is_(None)))
        ).scalars():
            self._index(tag)
        for a in (await self.session.execute(select(SiteTagAlias))).scalars():
            self.alias[a.name_lower] = a.tag_id
        self.disabled = {
            d.name_lower for d in (await self.session.execute(select(SiteTagDisabled))).scalars()
        }
        for g in (await self.session.execute(select(SiteTagGroup))).scalars():
            self.max_select[g.id] = g.max_select

    def _index(self, tag: SiteTag):
        self.by_id[tag.id] = tag
        if tag.group_id != FREE:
            self.official[(tag.group_id, tag.name.casefold())] = tag
        # 官方优先占名字（官方和自由不会同名，以防万一）
        current = self.by_lower.get(tag.name.casefold())
        if current is None or (current.group_id == FREE and tag.group_id != FREE):
            self.by_lower[tag.name.casefold()] = tag

    def target(self, tag: SiteTag) -> Target:
        return Target(tag.id, tag.name, tag.group_id, tag.group_id != FREE)

    async def ensure_official(self, group: str, name: str) -> SiteTag:
        tag = self.official.get((group, name.casefold()))
        if tag is None:
            tag = SiteTag(group_id=group, name=name, official=True, sort_order=999)
            self.session.add(tag)
            await self.session.flush()
            self._index(tag)
            self.report.stats["officialTagsCreated"] += 1
        return tag

    async def free(self, raw_name: str, res: Resolution) -> Target | None:
        """清洗后找已有 tag（官方 / 自由 / 同义词，撞名合并），没有就建自由 tag。"""
        name = clean_name(raw_name)
        if not name:
            res.notes.append(f"「{raw_name}」清洗后为空，丢弃")
            self.report.stats["droppedEmptyName"] += 1
            return None
        res.cleaned.append(name)
        lower = name.casefold()
        if lower in self.disabled:
            res.notes.append(f"「{name}」是禁用名，丢弃")
            self.report.stats["droppedDisabledName"] += 1
            return None
        if lower in self.alias and self.alias[lower] in self.by_id:
            tag = self.by_id[self.alias[lower]]
            res.notes.append(f"「{name}」是「{tag.name}」的同义词，合并")
            res.action = "merged"
            return self.target(tag)
        tag = self.by_lower.get(lower)
        if tag is not None:
            if tag.group_id != FREE:
                res.notes.append(f"「{name}」和官方 tag「{tag.group_id}/{tag.name}」同名，合并")
                res.action = "merged"
            elif tag.id not in self.created_free:
                res.notes.append(f"「{name}」和已有自由 tag 同名，合并")
                res.action = "merged"
            return self.target(tag)
        tag = SiteTag(group_id=FREE, name=name, official=False, created_by=None)
        self.session.add(tag)
        await self.session.flush()
        self._index(tag)
        self.created_free.add(tag.id)
        self.report.stats["freeTagsCreated"] += 1
        return self.target(tag)


async def _resolve(
    catalog: _Catalog, mapping: Mapping, *, legacy_id, name, source, category, is_abyss
) -> Resolution:
    native = source in ("discord", "fluxer") or (source == "custom" and category is None)
    res = Resolution(
        legacy_id=legacy_id,
        legacy_name=name,
        source=source,
        native=native,
        category=category,
        is_abyss=is_abyss,
    )
    if not native:
        # 自定义标签 7 类：全部变自由 tag，原名照搬（清洗）
        t = await catalog.free(name, res)
        if t:
            res.targets.append(t)
        else:
            res.action = "dropped"
        return res
    entry = mapping.entries.get(norm_key(name))
    if entry is None:
        res.pending = mapping.mark_pending
        if mapping.unmatched_as_free:
            t = await catalog.free(name, res)
            if t:
                res.targets.append(t)
            res.notes.append("对照表里没有")
            if res.action != "merged":
                res.action = "free" if t else "dropped"
        else:
            res.action = "dropped"
            res.notes.append("对照表里没有，按配置丢弃")
        return res
    if entry == [("drop", None)]:
        res.action = "dropped"
        res.notes.append("对照表：丢弃")
        return res
    res.action = "official"
    for group, tag_name in entry:
        if group == "drop":
            continue
        if group == FREE:
            t = await catalog.free(tag_name, res)
        else:
            tag = catalog.official.get((group, tag_name.casefold()))
            if tag is None:
                # 对照表写了目录里没有的官方 tag：退成自由 tag，并提醒管理员
                res.notes.append(f"官方目录里没有「{group}/{tag_name}」，先变自由 tag")
                catalog.report.stats["missingOfficialInCatalog"] += 1
                t = await catalog.free(tag_name, res)
            else:
                t = catalog.target(tag)
        if t and all(x.tag_id != t.tag_id for x in res.targets):
            res.targets.append(t)
    if res.targets and not any(t.official for t in res.targets):
        res.action = "free"
    return res


async def migrate(
    session_factory,
    *,
    mapping: Mapping | None = None,
    dry_run: bool = False,
    run_id: str | None = None,
    max_tags: int | None = None,
) -> Report:
    mapping = mapping or Mapping.load()
    run_id = run_id or utc_now().strftime("%Y%m%dT%H%M%SZ")
    report = Report(run_id=run_id, dry_run=dry_run)
    limit_total = min(mapping.max_tags, max_tags or mapping.max_tags)

    async with session_factory() as session:
        # 串行化：两个迁移同时跑会重复建自由 tag
        await session.execute(select(func.pg_advisory_xact_lock(7300731)))
        catalog = _Catalog(session, report)
        await catalog.load()
        pending_tag = await catalog.ensure_official(mapping.pending_group, mapping.pending_name)

        # ---- 1. 旧标签 → 新 tag ----
        legacy = (
            await session.execute(
                select(Tag).where(Tag.deleted_at.is_(None)).order_by(Tag.id)
            )
        ).scalars().all()
        resolutions: dict[int, Resolution] = {}
        for tag in legacy:
            res = await _resolve(
                catalog,
                mapping,
                legacy_id=tag.id,
                name=tag.name,
                source=tag.source,
                category=tag.category,
                is_abyss=bool(tag.is_abyss),
            )
            resolutions[tag.id] = res
        # 几个旧标签落到同一个新 tag：撞名合并
        by_target: dict[int, list[int]] = defaultdict(list)
        for lid, res in resolutions.items():
            for t in res.targets:
                by_target[t.tag_id].append(lid)
        for lid, res in resolutions.items():
            others = sorted(
                {o for t in res.targets for o in by_target[t.tag_id] if o != lid}
            )
            if others and res.action == "free":
                res.action = "merged"
            if others:
                res.notes.append(f"和旧标签 {others} 落到同一个新 tag")
        for lid, res in resolutions.items():
            report.stats[f"legacyTags.{res.action}"] += 1
            if res.category:
                report.stats[f"legacyTags.custom.{CATEGORY_NAMES[res.category]}"] += 1
            elif res.native:
                report.stats["legacyTags.native"] += 1
            entry = {"legacyTagId": str(lid), "action": res.action, **res.detail()}
            report.tags.append(entry)
            await _record(
                session,
                "tag",
                str(lid),
                ",".join(str(t.tag_id) for t in res.targets) or None,
                res.action,
                res.detail(),
                run_id,
            )

        # ---- 2. 作品 ----
        await _migrate_works(session, catalog, mapping, resolutions, pending_tag, limit_total, report)

        # ---- 3. 屏蔽标签 ----
        await _migrate_prefs(session, catalog, mapping, resolutions, legacy, pending_tag, report)

        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return report


async def _record(session, kind, legacy_key, target_key, action, detail, run_id):
    values = dict(
        kind=kind,
        legacy_key=legacy_key,
        target_key=target_key,
        action=action,
        detail=detail,
        run_id=run_id,
        updated_at=utc_now(),
    )
    await session.execute(
        insert(SiteTagMigrationRecord)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["kind", "legacy_key"],
            set_={k: v for k, v in values.items() if k not in ("kind", "legacy_key")},
        )
    )


@dataclass
class _Cand:
    target: Target
    legacy_id: int
    legacy_name: str
    native: bool
    upvotes: int
    added_at: datetime
    order: tuple


async def _migrate_works(session, catalog, mapping, resolutions, pending_tag, limit_total, report):
    # 自定义 / DC 同步绑定（当前生效的轮次）。作品：binding.work_id（触发器 capture_tag_work_identity 写的）；
    # 没写上的按 target_type=work 的 target_id、或帖子的 thread.work_id 补
    rows = (
        await session.execute(
            select(
                TagBinding.work_id,
                TagBinding.target_type,
                TagBinding.target_id,
                Thread.work_id,
                TagBinding.id,
                TagBinding.tag_id,
                TagBinding.upvotes,
                TagBinding.created_at,
            )
            .outerjoin(
                Thread,
                (TagBinding.target_type == "thread") & (Thread.id == TagBinding.target_id),
            )
            .where(
                TagBinding.ended_at.is_(None),
                or_(
                    TagBinding.work_id.is_not(None),
                    TagBinding.target_type == "work",
                    Thread.work_id.is_not(None),
                ),
            )
        )
    ).all()
    per_work: dict[int, list] = defaultdict(list)
    for work_id, target_type, target_id, thread_work, bid, tag_id, upvotes, created_at in rows:
        wid = work_id or (target_id if target_type == "work" else thread_work)
        per_work[int(wid)].append((tag_id, upvotes or 0, created_at, (0, bid)))
    native_rows = (
        await session.execute(
            select(WorkNativeTagBinding.work_id, WorkNativeTagSource.id, WorkNativeTagSource.tag_id)
            .join(WorkNativeTagSource, WorkNativeTagSource.id == WorkNativeTagBinding.source_id)
        )
    ).all()
    for work_id, sid, tag_id in native_rows:
        per_work[int(work_id)].append((tag_id, 0, None, (1, sid)))

    if not per_work:
        return
    works = {
        w.id: w
        for w in (
            await session.execute(
                select(Work).where(Work.id.in_(list(per_work)), Work.deleted_at.is_(None))
            )
        ).scalars()
    }
    existing_rows = (
        await session.execute(
            select(SiteWorkTag.work_id, SiteWorkTag.tag_id).where(
                SiteWorkTag.work_id.in_(list(works)), SiteWorkTag.removed_at.is_(None)
            )
        )
    ).all()
    existing: dict[int, set[int]] = defaultdict(set)
    for wid, tid in existing_rows:
        existing[wid].add(tid)
    now = utc_now()
    for work_id in sorted(per_work):
        work = works.get(work_id)
        if work is None:
            report.stats["works.skippedMissingOrDeleted"] += 1
            continue
        cands: dict[int, _Cand] = {}
        pending = False
        unknown_legacy = []
        for tag_id, upvotes, created_at, order in sorted(per_work[work_id], key=lambda r: r[3]):
            res = resolutions.get(tag_id)
            if res is None:
                unknown_legacy.append(tag_id)  # 旧标签已删除
                continue
            pending = pending or res.pending
            for t in res.targets:
                c = cands.get(t.tag_id)
                if c is None:
                    cands[t.tag_id] = _Cand(
                        t, tag_id, res.legacy_name, res.native, upvotes, created_at or now, order
                    )
                else:
                    c.upvotes = max(c.upvotes, upvotes)
        if pending:
            cands.setdefault(
                pending_tag.id,
                _Cand(catalog.target(pending_tag), 0, "（待分类）", True, 0, now, (9, 0)),
            )
        have = set(existing[work_id])
        group_count: dict[str, int] = defaultdict(int)
        for tid in have:
            tag = catalog.by_id.get(tid)
            if tag is not None:
                group_count[tag.group_id] += 1
        officials = sorted((c for c in cands.values() if c.target.official), key=lambda c: c.order)
        frees = sorted(
            (c for c in cands.values() if not c.target.official),
            key=lambda c: (-c.upvotes, c.added_at, c.order),
        )
        added, already, over_limit, group_limit = [], [], [], []
        total = len(have)
        for c in officials + frees:
            t = c.target
            brief = {"id": str(t.tag_id), "name": t.name, "group": t.group, "from": c.legacy_name}
            if t.tag_id in have:
                already.append(brief)
                continue
            cap = catalog.max_select.get(t.group)
            if t.official and cap is not None and group_count[t.group] >= cap:
                group_limit.append(brief)
                continue
            if total >= limit_total:
                over_limit.append({**brief, "upvotes": c.upvotes})
                continue
            session.add(
                SiteWorkTag(
                    work_id=work_id,
                    tag_id=t.tag_id,
                    # 官方 tag 和 DC 原生标签算作者加的；自定义标签算迁移系统加的
                    added_by=work.author_id if (t.official or c.native) else None,
                    created_at=now,
                )
            )
            have.add(t.tag_id)
            group_count[t.group] += 1
            total += 1
            added.append(brief)
        report.stats["works.processed"] += 1
        report.stats["works.tagsAdded"] += len(added)
        if over_limit:
            report.stats["works.overLimit"] += 1
            report.stats["works.tagsDroppedOverLimit"] += len(over_limit)
        if group_limit:
            report.stats["works.tagsDroppedGroupLimit"] += len(group_limit)
        if pending:
            report.stats["works.pending"] += 1
        if not added:
            report.stats["works.unchanged"] += 1
        detail = {
            "added": added,
            "alreadyPresent": already,
            "droppedOverLimit": over_limit,
            "droppedGroupLimit": group_limit,
            "pending": pending,
            "deletedLegacyTagIds": [str(x) for x in unknown_legacy],
            "totalAfter": total,
        }
        report.works.append({"workId": str(work_id), **detail})
        await _record(
            session,
            "work",
            str(work_id),
            str(work_id),
            "migrated" if added else "unchanged",
            detail,
            report.run_id,
        )
    await session.flush()


async def _migrate_prefs(session, catalog, mapping, resolutions, legacy, pending_tag, report):
    rows = (
        await session.execute(
            select(UserSearchPreferences).where(UserSearchPreferences.exclude_tags.is_not(None))
        )
    ).scalars().all()
    rows = [r for r in rows if r.exclude_tags]
    if not rows:
        return
    by_name: dict[str, list[int]] = defaultdict(list)
    for tag in legacy:
        by_name[tag.name].append(tag.id)
    fluxer_ids = {r.fluxer_user_id for r in rows if r.fluxer_user_id}
    known_users = set(
        (
            await session.execute(
                select(User.id).where(User.id.in_(fluxer_ids or {""}), User.merged_into_id.is_(None))
            )
        ).scalars()
    )
    discord_ids = {str(r.user_id) for r in rows if r.user_id}
    by_discord = {
        d: u
        for d, u in (
            await session.execute(
                select(DiscordIdentity.discord_user_id, DiscordIdentity.user_id).where(
                    DiscordIdentity.discord_user_id.in_(discord_ids or {""})
                )
            )
        ).all()
    }
    extra: dict[str, Resolution] = {}
    for row in sorted(rows, key=lambda r: r.id):
        user_id = row.fluxer_user_id if row.fluxer_user_id in known_users else None
        if user_id is None and row.user_id:
            user_id = by_discord.get(str(row.user_id))
        new_ids: list[str] = []
        missing: list[str] = []
        for name in row.exclude_tags:
            ids = by_name.get(name)
            res_list = [resolutions[i] for i in ids or [] if i in resolutions]
            if not res_list:
                # 旧库里没有这个标签（可能早删了）：按原生标签的对照表再试一次
                if name not in extra:
                    extra[name] = await _resolve(
                        catalog, mapping, legacy_id=None, name=name, source="discord",
                        category=None, is_abyss=False,
                    )
                res = extra[name]
                res_list = [res] if res.targets and not res.pending else []
            got = [str(t.tag_id) for r in res_list for t in r.targets if t.tag_id != pending_tag.id]
            if not got:
                missing.append(name)
            new_ids.extend(got)
        new_ids = list(dict.fromkeys(new_ids))
        detail = {
            "legacyUserId": str(row.user_id) if row.user_id else None,
            "fluxerUserId": row.fluxer_user_id,
            "excludeTags": list(row.exclude_tags),
            "blockedTagIds": new_ids,
            "missingNames": missing,
            "skippedFollowed": [],
        }
        if user_id is None:
            action = "user_not_found"
        else:
            prefs = await session.get(SiteUserPrefs, user_id, with_for_update=True)
            if prefs is None:
                prefs = SiteUserPrefs(user_id=user_id)
                session.add(prefs)
            followed = set(prefs.followed_tag_ids or [])
            current = list(prefs.blocked_tag_ids or [])
            added = []
            for tid in new_ids:
                if tid in followed:
                    detail["skippedFollowed"].append(tid)
                elif tid not in current:
                    current.append(tid)
                    added.append(tid)
            if added:
                prefs.blocked_tag_ids = current
                prefs.updated_at = utc_now()
            detail["added"] = added
            action = "migrated" if added else "unchanged"
            report.stats["prefs.blockedTagsAdded"] += len(added)
        report.stats[f"prefs.{action}"] += 1  # migrated / unchanged / user_not_found
        report.prefs.append({"prefsId": str(row.id), "userId": user_id, "action": action, **detail})
        await _record(session, "prefs", str(row.id), user_id, action, detail, report.run_id)
    await session.flush()


def write_json(report: Report, path: Path | str) -> None:
    Path(path).write_text(
        json.dumps(report.as_json(), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

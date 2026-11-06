"""协议 tag 目录和打 tag 规则（01 号文档第七节 + tag 方案的必选、名额）。

规则照搬 web 仓库 src/shared/tags/rules.ts（契约测试 mock-server/vendor 里那份），
前端发布页和这里用同一套判定：
- 组规则：onlyForTypes 决定组出不出现；required 对所有类型必选，requiredForTypes 按类型必选；
  tag 的 forTypes 限制可选范围；maxSelect=1 的组再加一个是替换；
- 总数上限 maxTags（配置 SITE_MAX_TAGS，默认 15）；
- 换资源类型后不再适用的 tag 自动去掉（reconcile）；
- 必答问题回答「含」自动打上 restrictsTagId，回答「不含」却打着那个 tag 算矛盾。

目录从 site_tag_group / site_tag / site_tag_question 读（每次请求现读，几十行）。
作品上的 tag ID 是 site_tag.id 的字符串；自由 tag 也是 site_tag 的行（group_id=free）。
"""

from dataclasses import dataclass, field

from sqlalchemy import select

from models.site_account import SiteSetting
from models.site_tag import SiteTag, SiteTagGroup, SiteTagQuestion
from site_v1.config import get_config

FREE = "free"
MAX_FREE_TAG_LENGTH = 20
MAX_TITLE_LENGTH = 60
MAX_SUMMARY_LENGTH = 200
RATING_LABEL = {"general": "全年龄", "r18": "R18", "r18g": "R18G"}
_RANK = {"general": 0, "r18": 1, "r18g": 2}


@dataclass
class TagInfo:
    id: str
    name: str
    group: str
    restricts: bool = False
    official: bool = False
    rating: str | None = None
    for_types: list[str] | None = None


@dataclass
class GroupDef:
    id: str
    name: str
    required: bool
    max_select: int
    only_for_types: list[str] | None
    required_for_types: list[str] | None
    tags: list[TagInfo] = field(default_factory=list)


@dataclass
class Question:
    id: str
    text: str
    hint: str
    for_types: list[str]
    restricts_tag_id: str


@dataclass
class Catalog:
    version: str
    max_tags: int
    groups: list[GroupDef]
    questions: list[Question]
    by_id: dict[str, TagInfo] = field(default_factory=dict)
    """官方 tag，外加按需补进来的自由 tag（remember）。"""

    def info(self, tag_id: str) -> TagInfo | None:
        return self.by_id.get(tag_id)

    def remember(self, tag: TagInfo) -> None:
        self.by_id.setdefault(tag.id, tag)

    def group(self, group_id: str) -> GroupDef | None:
        return next((g for g in self.groups if g.id == group_id), None)

    def official_by_name(self, name: str) -> TagInfo | None:
        key = name.strip().lower()
        if not key:
            return None
        for g in self.groups:
            for t in g.tags:
                if t.name.lower() == key:
                    return t
        return None


def tag_info_of(row: SiteTag) -> TagInfo:
    return TagInfo(
        id=str(row.id),
        name=row.name,
        group=row.group_id,
        restricts=bool(row.restricts),
        official=bool(row.official) and row.group_id != FREE,
        rating=row.rating,
        for_types=list(row.for_types) if row.for_types else None,
    )


async def load_catalog(session) -> Catalog:
    groups = (
        (await session.execute(select(SiteTagGroup).order_by(SiteTagGroup.sort_order)))
        .scalars()
        .all()
    )
    tags = (
        (
            await session.execute(
                select(SiteTag)
                .where(SiteTag.group_id != FREE, SiteTag.deleted_at.is_(None))
                .order_by(SiteTag.sort_order, SiteTag.id)
            )
        )
        .scalars()
        .all()
    )
    questions = (
        (
            await session.execute(
                select(SiteTagQuestion).order_by(SiteTagQuestion.sort_order)
            )
        )
        .scalars()
        .all()
    )
    setting = await session.get(SiteSetting, "tag_catalog")
    version = str(((setting.value or {}) if setting else {}).get("version") or "0")
    defs = {
        g.id: GroupDef(
            id=g.id,
            name=g.name,
            required=bool(g.required),
            max_select=max(1, int(g.max_select or 1)),
            only_for_types=list(g.only_for_types) if g.only_for_types else None,
            required_for_types=list(g.required_for_types)
            if g.required_for_types
            else None,
        )
        for g in groups
    }
    catalog = Catalog(
        version=version,
        max_tags=get_config().max_tags,
        groups=list(defs.values()),
        questions=[
            Question(
                id=q.id,
                text=q.text,
                hint=q.hint or "",
                for_types=list(q.for_types or []),
                restricts_tag_id=str(q.restricts_tag_id),
            )
            for q in questions
        ],
    )
    for row in tags:
        info = tag_info_of(row)
        if row.group_id in defs:
            defs[row.group_id].tags.append(info)
            catalog.by_id[info.id] = info
    return catalog


def catalog_dto(catalog: Catalog) -> dict:
    """协议 TagCatalog。"""
    groups = []
    for g in catalog.groups:
        item = {
            "id": g.id,
            "name": g.name,
            "required": g.required,
            "maxSelect": g.max_select,
            "tags": [],
        }
        if g.only_for_types:
            item["onlyForTypes"] = list(g.only_for_types)
        if g.required_for_types:
            item["requiredForTypes"] = list(g.required_for_types)
        for t in g.tags:
            tag = {"id": t.id, "name": t.name, "restricts": t.restricts}
            if g.id == "rating":
                tag["rating"] = t.rating or rating_of_tag_name(t.name)
            if t.for_types:
                tag["forTypes"] = list(t.for_types)
            item["tags"].append(tag)
        groups.append(item)
    return {
        "version": catalog.version,
        "maxTags": catalog.max_tags,
        "groups": groups,
        "requiredQuestions": [
            {
                "id": q.id,
                "text": q.text,
                "hint": q.hint,
                "forTypes": list(q.for_types),
                "restrictsTagId": q.restricts_tag_id,
            }
            for q in catalog.questions
        ],
    }


# ---------------- 查询 ----------------


def rating_of_tag_name(name: str) -> str:
    key = name.strip().upper()
    if key == "R18G":
        return "r18g"
    if key == "R18":
        return "r18"
    return "general"


def rating_rank(rating: str | None) -> int:
    return _RANK.get(rating or "general", 0)


def _intersects(a, b) -> bool:
    return any(x in b for x in a)


def selected_type_names(catalog: Catalog, ids) -> list[str]:
    group = catalog.group("type")
    if group is None:
        return []
    return [t.name for t in group.tags if t.id in ids]


@dataclass
class GroupRule:
    id: str
    name: str
    visible: bool
    required: bool
    max: int
    single: bool
    options: list[TagInfo]


def group_rule(group: GroupDef, types: list[str]) -> GroupRule:
    options = [
        t for t in group.tags if not t.for_types or _intersects(types, t.for_types)
    ]
    visible = (
        not group.only_for_types or _intersects(types, group.only_for_types)
    ) and bool(options)
    required = visible and (
        group.required
        or (
            bool(group.required_for_types)
            and _intersects(types, group.required_for_types)
        )
    )
    return GroupRule(
        id=group.id,
        name=group.name,
        visible=visible,
        required=required,
        max=group.max_select,
        single=group.max_select == 1,
        options=options,
    )


def ids_in_group(catalog: Catalog, ids, group_id: str) -> list[str]:
    out = []
    for i in ids:
        info = catalog.info(i)
        if info is not None and info.group == group_id:
            out.append(i)
    return out


def is_applicable(catalog: Catalog, tag_id: str, types: list[str]) -> bool:
    info = catalog.info(tag_id)
    if info is None:
        return False
    if info.group == FREE:
        return True
    group = catalog.group(info.group)
    if group is None:
        return False
    rule = group_rule(group, types)
    return rule.visible and any(o.id == tag_id for o in rule.options)


def reconcile(catalog: Catalog, ids) -> list[str]:
    """去掉重复的、目录里没有的、当前资源类型下不适用的 tag。"""
    types = selected_type_names(catalog, ids)
    seen: set[str] = set()
    out = []
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        if is_applicable(catalog, i, types):
            out.append(i)
    return out


def validate_free_tag_name(name: str) -> str | None:
    if not name:
        return "tag 不能为空"
    if any(ch.isspace() for ch in name):
        return "tag 里不能有空格"
    if len(name) > MAX_FREE_TAG_LENGTH:
        return f"tag 最长 {MAX_FREE_TAG_LENGTH} 个字"
    if not all(ch.isalnum() or ch == "·" for ch in name):
        return "tag 里不能有特殊符号"
    return None


# ---------------- 增删 ----------------


@dataclass
class TagChange:
    ok: bool
    ids: list[str]
    error: str | None = None
    replaced: str | None = None


def add_tag_id(catalog: Catalog, ids, tag_id: str) -> TagChange:
    current = list(ids)
    info = catalog.info(tag_id)
    if info is None:
        return TagChange(False, current, "没有这个 tag")
    if tag_id in current:
        return TagChange(False, current, f"「{info.name}」已经选了")
    if info.group == FREE:
        error = validate_free_tag_name(info.name)
        if error:
            return TagChange(False, current, error)
        if len(current) >= catalog.max_tags:
            return TagChange(
                False, current, f"最多 {catalog.max_tags} 个 tag，先移除一些再加"
            )
        return TagChange(True, [*current, tag_id])
    types = selected_type_names(catalog, current)
    group = catalog.group(info.group)
    if group is None:
        return TagChange(False, current, "没有这个 tag")
    # 资源类型本身不受 forTypes 约束；其他组按已选类型判断
    if info.group != "type" and not is_applicable(catalog, tag_id, types):
        need = info.for_types or group.only_for_types or []
        return TagChange(
            False,
            current,
            f"「{info.name}」只适用于{'、'.join(need)}，先选对应的资源类型",
        )
    rule = group_rule(group, types)
    in_group = ids_in_group(catalog, current, info.group)
    if rule.single and in_group:
        replaced = in_group[0]
        nxt = [tag_id if x == replaced else x for x in current]
        return TagChange(True, reconcile(catalog, nxt), replaced=replaced)
    if len(current) >= catalog.max_tags:
        return TagChange(
            False, current, f"最多 {catalog.max_tags} 个 tag，先移除一些再加"
        )
    if len(in_group) >= rule.max:
        return TagChange(False, current, f"{group.name}最多选 {rule.max} 个")
    return TagChange(True, reconcile(catalog, [*current, tag_id]))


def remove_tag_id(catalog: Catalog, ids, tag_id: str) -> list[str]:
    return reconcile(catalog, [x for x in ids if x != tag_id])


# ---------------- 校验 ----------------


def _required_message(rule: GroupRule, group: GroupDef) -> str:
    if rule.id == "rating":
        return "请选择分级"
    if rule.id == "type":
        return "请选择资源类型"
    only = group.required_for_types or group.only_for_types
    return f"{'、'.join(only)}需要选择{rule.name}" if only else f"请选择{rule.name}"


def group_issues(catalog: Catalog, ids, group_id: str) -> list[dict]:
    group = catalog.group(group_id)
    if group is None:
        return []
    rule = group_rule(group, selected_type_names(catalog, ids))
    count = len(ids_in_group(catalog, ids, group_id))
    issues = []
    if rule.required and count < 1:
        issues.append(
            {
                "code": "required",
                "group": group_id,
                "message": _required_message(rule, group),
            }
        )
    if count > rule.max:
        issues.append(
            {
                "code": "too_many_in_group",
                "group": group_id,
                "message": f"{rule.name}最多选 {rule.max} 个，现在有 {count} 个",
            }
        )
    return issues


def validate_tag_ids(catalog: Catalog, ids) -> list[dict]:
    issues = []
    types = selected_type_names(catalog, ids)
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            issues.append({"code": "duplicate", "message": f"tag 重复：{i}"})
            continue
        seen.add(i)
        info = catalog.info(i)
        if info is None:
            issues.append({"code": "unknown_tag", "message": f"没有这个 tag：{i}"})
            continue
        if info.group == FREE:
            error = validate_free_tag_name(info.name)
            if error:
                issues.append(
                    {
                        "code": "invalid_free_tag",
                        "group": FREE,
                        "message": f"「{info.name}」：{error}",
                    }
                )
            continue
        if not is_applicable(catalog, i, types):
            issues.append(
                {
                    "code": "not_applicable",
                    "group": info.group,
                    "message": f"「{info.name}」不适用于当前的资源类型",
                }
            )
    for g in catalog.groups:
        issues.extend(group_issues(catalog, ids, g.id))
    if len(ids) > catalog.max_tags:
        issues.append(
            {
                "code": "too_many_total",
                "message": f"最多 {catalog.max_tags} 个 tag，现在有 {len(ids)} 个",
            }
        )
    return issues


# ---------------- 分级 ----------------


def rating_from_ids(catalog: Catalog, ids) -> str | None:
    found = ids_in_group(catalog, ids, "rating")
    if not found:
        return None
    info = catalog.info(found[0])
    return info.rating or rating_of_tag_name(info.name)


def has_restricting(catalog: Catalog, ids) -> bool:
    return any((catalog.info(i) or TagInfo("", "", "")).restricts for i in ids)


# ---------------- 必答问题 ----------------


def applicable_questions(catalog: Catalog, ids) -> list[Question]:
    types = selected_type_names(catalog, ids)
    return [
        q
        for q in catalog.questions
        if not q.for_types or _intersects(types, q.for_types)
    ]


def apply_answers(catalog: Catalog, ids, answers: list[dict]):
    """返回 (套用回答后的 tag, 问题清单)。"""
    nxt = list(ids)
    issues = []
    for q in applicable_questions(catalog, ids):
        a = next((x for x in answers if x.get("question") == q.id), None)
        tag = catalog.info(q.restricts_tag_id)
        tag_name = tag.name if tag else q.restricts_tag_id
        if a is None or not isinstance(a.get("answer"), bool):
            issues.append(
                {
                    "code": "answer_missing",
                    "field": f"answers.{q.id}",
                    "message": f"请回答：{q.text}",
                }
            )
        elif a["answer"] and q.restricts_tag_id not in nxt:
            nxt.append(q.restricts_tag_id)
        elif not a["answer"] and q.restricts_tag_id in nxt:
            issues.append(
                {
                    "code": "answer_conflict",
                    "field": f"answers.{q.id}",
                    "message": f"回答了「不含」，但作品打着「{tag_name}」tag，请改其中一个",
                }
            )
    return nxt, issues


def issues_to_fields(issues: list[dict]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for issue in issues:
        key = issue["field"]
        fields[key] = (
            f"{fields[key]}；{issue['message']}" if key in fields else issue["message"]
        )
    return fields

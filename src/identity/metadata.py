"""新建数据库与在线运行迁移使用相同的身份约束。"""

from sqlalchemy import Index, UniqueConstraint, CheckConstraint
from sqlmodel import SQLModel
from identity.reference_manifest import REFERENCES, fluxer_column


def apply_identity_constraints():
    """从旧唯一约束推导统一身份约束，保留来源字段供审计。"""
    for name, columns in REFERENCES.items():
        table = SQLModel.metadata.tables[name]
        candidates = [
            constraint
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        ]
        # Discord 作者资料允许多个历史身份投影到同一统一作者。
        if name == "author":
            continue
        for number, constraint in enumerate(candidates):
            original = [column.name for column in constraint.columns]
            if not any(column in columns for column in original):
                continue
            canonical = [
                fluxer_column(column) if column in columns else column
                for column in original
            ]
            if any(
                isinstance(existing, UniqueConstraint)
                and [column.name for column in existing.columns] == canonical
                for existing in table.constraints
            ):
                continue
            table.append_constraint(
                UniqueConstraint(*canonical, name=f"uq_fluxer_{name}_{number}")
            )
        for number, index in enumerate(list(table.indexes)):
            original = [column.name for column in index.columns]
            if not index.unique or not any(column in columns for column in original):
                continue
            canonical = [
                fluxer_column(column) if column in columns else column
                for column in original
            ]
            Index(
                f"ux_fluxer_{name}_{number}",
                *[table.c[column] for column in canonical],
                unique=True,
                postgresql_where=index.dialect_options["postgresql"].get("where"),
            )
    table = SQLModel.metadata.tables["author_follow"]
    table.append_constraint(
        CheckConstraint(
            "fluxer_user_id <> fluxer_author_id",
            name="ck_fluxer_author_follow_not_self",
        )
    )

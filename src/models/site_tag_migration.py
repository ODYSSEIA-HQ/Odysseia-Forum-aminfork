"""旧标签迁成协议 tag 的迁移记录（协议 7.3，scripts/migrate_legacy_tags.py 写）。

一张表记三类记录，按 (kind, legacy_key) 唯一，重跑时覆盖成最新结果（脚本幂等）：
- kind=tag    一个旧标签（tag.id）迁成了什么：legacy_key=旧 tag.id；
              action = official（按对照表归入官方组）/ free（变自由 tag）/ merged（清洗后和已有 tag 撞名，合并）
              / dropped（对照表里标了丢弃，或清洗后为空、名字被禁用）；
              detail 里有原名、来源（discord/fluxer/custom）、原分类（癖好、作品……）、是否深渊向、清洗后名字、新 tag ID。
- kind=work   一个作品：legacy_key=work.id；detail 里有加上的 tag、超过 15 个丢掉的、题材超 3 个丢掉的、是否打了「待分类」。
- kind=prefs  一个用户的屏蔽标签：legacy_key=旧偏好行 id；detail 里有原屏蔽名、换成的新 tag ID、找不到的名字。
给管理员升级自由 tag 时参考「原分类」：kind=tag AND action IN ('free','merged') 的 detail.legacyCategory。
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column, Index
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class SiteTagMigrationRecord(SQLModel, table=True):
    __tablename__ = "site_tag_migration_record"
    __table_args__ = (
        Index("uq_site_tag_migration_record", "kind", "legacy_key", unique=True),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """记录编号。"""

    kind: str
    """tag / work / prefs。"""

    legacy_key: str
    """旧数据的主键（字符串）。"""

    target_key: str | None = None
    """新数据：tag 记录是新 tag ID（逗号分隔，可能一拆多），work 记录是作品 ID，prefs 是平台用户 ID。"""

    action: str
    """见模块说明。"""

    detail: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    """明细（见模块说明）。"""

    run_id: str
    """哪一次迁移写的（时间戳），重跑覆盖。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """最近一次写入时间。"""

"""/api/v1「我的」、提醒、公告规则、作品内确认/规则/钉选、DC 存档、书单补充、好友用的新表。

表结构以 models/site_me.py 为准（这里按模型建表，不改已有表）。
"""

from alembic import op

revision = "site_v1_me_tables"
down_revision = "site_v1_chat_tables"
branch_labels = None
depends_on = None


def _tables():
    from models import site_me as m

    return [
        m.SiteUserPreferencesDoc,
        m.SiteTagHabits,
        m.SitePinnedWorks,
        m.SiteDecorationRole,
        m.SiteUserDecorationGrant,
        m.SiteUserDecorationPref,
        m.SitePluginToken,
        m.SiteWorkAck,
        m.SiteWorkRules,
        m.SiteWorkPin,
        m.SiteDcArchivePost,
        m.SiteAnnouncement,
        m.SiteAnnouncementAck,
        m.SiteRuleDoc,
        m.SiteNotification,
        m.SiteCollectionMeta,
        m.SiteFriend,
        m.SiteFriendRequest,
    ]


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    for model in _tables():
        model.__table__.create(bind=bind, checkfirst=False)


def downgrade():
    bind = op.get_bind()
    for model in reversed(_tables()):
        model.__table__.drop(bind=bind, checkfirst=True)

from models.user import User
from models.forum_nsfw_sync_state import ForumNsfwSyncState
from models.discord_identity import DiscordIdentity
from models.discord_tag_source import DiscordTagSource
from models.discord_tag_sync_state import DiscordTagSyncState
from models.author import Author
from models.banner_application import BannerApplication
from models.banner_carousel import BannerCarousel
from models.channel import Channel
from models.banner_waitlist import BannerWaitlist
from models.booklist import Booklist
from models.booklist_item import BooklistItem
from models.booklist_publish import BooklistPublish
from models.bot_config import BotConfig
from models.mutex_tag_group import MutexTagGroup
from models.mutex_tag_rule import MutexTagRule
from models.thread_tag_link import ThreadTagLink
from models.tag import Tag
from models.thread import Thread
from models.thread_follow import ThreadFollow
from models.user_collection import UserCollection
from models.user_search_preferences import UserSearchPreferences
from models.user_update_preference import UserUpdatePreference
from models.author_follow import AuthorFollow
from models.notification import Notification
from models.thread_update import ThreadUpdate
from models.tag_alias import TagAlias
from models.tag_relation import TagRelation
from models.tag_binding import TagBinding
from models.tag_vote import TagVote
from models.tag_proposal import TagProposal
from models.tag_proposal_block import TagProposalBlock
from models.operation_log import OperationLog
from models.tag_notification_task import TagNotificationTask
from identity.metadata import apply_identity_constraints
from models.work import Work
from models.work_statistics import WorkStatistics
from models.work_reaction import WorkReaction
from models.work_task import WorkTask
from models.work_announcement import WorkAnnouncement
from models.work_event import WorkEvent
from models.work_user_mute import WorkUserMute
from models.work_source_state import WorkSourceState
from models.work_native_tag_source import WorkNativeTagSource
from models.work_native_tag_binding import WorkNativeTagBinding
from models.site_account import (
    SiteAccount,
    SiteFakeCredential,
    SiteRestrictedAccess,
    SiteSetting,
    SiteUserBlock,
    SiteUserPrefs,
)
from models.site_tag import SiteTag, SiteTagGroup, SiteTagQuestion, SiteWorkTag
from models.work_version import WorkVersion, WorkVersionFile
from models.attachment import Attachment
from models.site_moderation import ModerationQueueItem, Report, UserModerationRecord
# 活动（3.22）、娱乐区（/fun）：events 块
from models.site_event import SiteEvent, SiteEventEntry, SiteEventFollow, SiteEventLog  # noqa: F401
from models.site_fun import (  # noqa: F401
    SiteFunBlackjackRound,
    SiteFunBlackjackShoe,
    SiteFunMarket,
    SiteFunStockAccount,
    SiteFunStockBar,
    SiteFunStockNews,
    SiteFunStockPosition,
    SiteFunStockTrade,
    SiteFunWallet,
)

__all__ = [
    "Attachment",
    "ModerationQueueItem",
    "Report",
    "SiteAccount",
    "SiteFakeCredential",
    "SiteRestrictedAccess",
    "SiteSetting",
    "SiteTag",
    "SiteTagGroup",
    "SiteTagQuestion",
    "SiteUserBlock",
    "SiteUserPrefs",
    "SiteWorkTag",
    "UserModerationRecord",
    "WorkVersion",
    "WorkVersionFile",
    "Author",
    "AuthorFollow",
    "BannerApplication",
    "BannerCarousel",
    "BannerWaitlist",
    "Booklist",
    "BooklistItem",
    "BooklistPublish",
    "BotConfig",
    "Channel",
    "DiscordIdentity",
    "DiscordTagSource",
    "DiscordTagSyncState",
    "MutexTagGroup",
    "MutexTagRule",
    "Notification",
    "OperationLog",
    "Tag",
    "TagAlias",
    "TagBinding",
    "TagNotificationTask",
    "TagProposal",
    "TagProposalBlock",
    "TagRelation",
    "TagVote",
    "Thread",
    "ThreadFollow",
    "ThreadTagLink",
    "ThreadUpdate",
    "User",
    "ForumNsfwSyncState",
    "UserCollection",
    "UserSearchPreferences",
    "UserUpdatePreference",
    "Work",
    "WorkAnnouncement",
    "WorkEvent",
    "WorkNativeTagBinding",
    "WorkNativeTagSource",
    "WorkReaction",
    "WorkSourceState",
    "WorkStatistics",
    "WorkTask",
    "WorkUserMute",
]

apply_identity_constraints()

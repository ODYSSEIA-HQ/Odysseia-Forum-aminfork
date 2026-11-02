from models.user import User
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

__all__ = [
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
    "UserCollection",
    "UserSearchPreferences",
    "UserUpdatePreference",
]

apply_identity_constraints()

"""保留 Discord 原始身份的显式 Fluxer 投影列。"""

REFERENCES = {
    "author": ["id"],
    "thread": ["author_id"],
    "booklist": ["owner_id"],
    "booklist_item": ["owner_id"],
    "booklist_publish": ["discord_user_id"],
    "author_follow": ["user_id", "author_id"],
    "thread_follow": ["user_id"],
    "user_collection": ["user_id"],
    "user_search_preferences": ["user_id"],
    "user_update_preference": ["user_id"],
    "notification": ["user_id"],
    "thread_update": ["publisher_id"],
    "banner_application": ["applicant_id", "reviewer_id"],
    "banner_waitlist": ["applicant_id"],
    "banner_carousel": ["applicant_id"],
    "bot_config": ["update_user_id"],
    "tag_binding": ["actor_id"],
    "tag_vote": ["user_id"],
    "tag_proposal": ["applicant_id", "owner_id"],
    "operation_log": ["actor_id"],
}

AUTHOR_LISTS = {"user_search_preferences": ["include_authors", "exclude_authors"]}


def fluxer_column(column):
    """返回不会与 Discord 列混淆的 Fluxer 字段名。"""
    return (
        "fluxer_user_id" if column in {"id", "discord_user_id"} else "fluxer_" + column
    )


# 业务归属可沿显式管理合并链移动，历史操作者和发布者不改写。
OWNERSHIP_REFERENCES = {
    "author": ["id"],
    "thread": ["author_id"],
    "booklist": ["owner_id"],
    "booklist_item": ["owner_id"],
    "author_follow": ["user_id", "author_id"],
    "thread_follow": ["user_id"],
    "user_collection": ["user_id"],
    "user_search_preferences": ["user_id"],
    "user_update_preference": ["user_id"],
    "notification": ["user_id"],
    "tag_vote": ["user_id"],
    "tag_proposal": ["owner_id"],
}

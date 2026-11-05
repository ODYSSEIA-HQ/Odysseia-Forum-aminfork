import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _setup_hook_cog_names() -> tuple[str, ...]:
    tree = ast.parse((ROOT / "bot_main.py").read_text(encoding="utf-8"))
    bot_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MyBot"
    )
    setup_hook = next(
        node
        for node in bot_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "setup_hook"
    )
    cog_assignment = next(
        node
        for node in setup_hook.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "cogs_to_load"
            for target in node.targets
        )
    )
    assert isinstance(cog_assignment.value, ast.List)
    return tuple(
        call.func.id
        for call in cog_assignment.value.elts
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    )


def _setup_hook_keyword_value(target_name: str, keyword_name: str) -> object:
    tree = ast.parse((ROOT / "bot_main.py").read_text(encoding="utf-8"))
    bot_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MyBot"
    )
    setup_hook = next(
        node
        for node in bot_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "setup_hook"
    )
    assignment = next(
        node
        for node in setup_hook.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == target_name
            for target in node.targets
        )
    )
    assert isinstance(assignment.value, ast.Call)
    keyword = next(
        keyword
        for keyword in assignment.value.keywords
        if keyword.arg == keyword_name
    )
    assert isinstance(keyword.value, ast.Constant)
    return keyword.value.value


def test_fluxer_acceptance_skips_auditor_and_backup_but_keeps_other_cogs():
    cog_names = set(_setup_hook_cog_names())

    assert "Auditor" not in cog_names
    assert "BackupCog" not in cog_names
    assert _setup_hook_keyword_value(
        "sync_service", "enable_discord_thread_sync"
    ) is False
    assert _setup_hook_keyword_value(
        "cache_service", "enable_discord_channel_refresh"
    ) is False
    assert {
        "ThreadManager",
        "Indexer",
        "Search",
        "Preferences",
        "Configuration",
        "BannerManagement",
        "BannerEventListener",
        "CollectionCog",
        "UpdateDetector",
        "AuthorCog",
        "TagCog",
    } <= cog_names


def test_fluxer_acceptance_disables_discord_image_refresh_api():
    tree = ast.parse((ROOT / "api_main.py").read_text(encoding="utf-8"))
    configure_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "configure_fetch_images_router"
    )
    option = next(
        keyword
        for keyword in configure_call.keywords
        if keyword.arg == "enable_discord_refresh"
    )

    assert isinstance(option.value, ast.Constant)
    assert option.value.value is False

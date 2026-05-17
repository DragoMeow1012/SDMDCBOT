"""
Commands 套件入口。
呼叫 setup_all(tree) 將所有指令群組與指令注冊到 CommandTree。
"""
from discord import app_commands

from commands import (
    admin,
    ai,
    image_search,
    quote,
    pixiv,
    nhentai,
    translate,
    relationship,
    tool,
    rank,
    daily_mom,
    electrocute,
    morning,
    gamble,
)


def setup_all(tree: app_commands.CommandTree) -> None:
    admin.setup(tree)
    ai.setup(tree)
    image_search.setup(tree)
    quote.setup(tree)
    pixiv.setup(tree)
    nhentai.setup(tree)
    translate.setup(tree)
    relationship.setup(tree)
    tool.setup(tree)
    rank.setup(tree)
    daily_mom.setup(tree)
    electrocute.setup(tree)
    morning.setup(tree)
    gamble.setup(tree)

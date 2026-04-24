from typing import Optional


class ActionsContainer:
    def __init__(self, client):
        self.guild = GuildActions(client)


class GuildActions:
    def __init__(self, client):
        from lib.actions.guild.join import JoinHandler
        self._join_handler = JoinHandler(client)

    async def join(self, invite_code: str, proxy: Optional[str] = None):
        return await self._join_handler.join_guild(invite_code, proxy=proxy)
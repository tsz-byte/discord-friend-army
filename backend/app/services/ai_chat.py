from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.core.config import get_settings


AGENT_TOOL_ROUTES: dict[str, tuple[str, str]] = {
    'channel__send_embed': ('POST', '/api/v1/agent/channel/send-embed'),
    'channel__send_message': ('POST', '/api/v1/agent/channel/send-message'),
    'channel__send_dm': ('POST', '/api/v1/agent/channel/send-dm'),
    'channel__delete': ('POST', '/api/v1/agent/channel/delete'),
    'channel__create': ('POST', '/api/v1/agent/channel/create'),
    'channel__edit': ('PATCH', '/api/v1/agent/channel/edit'),
    'channel__list': ('GET', '/api/v1/agent/channel/list'),
    'channel__bulk_send': ('POST', '/api/v1/agent/channel/bulk-send'),
    'message__delete': ('POST', '/api/v1/agent/message/delete'),
    'message__bulk_delete': ('POST', '/api/v1/agent/message/bulk-delete'),
    'message__edit': ('PATCH', '/api/v1/agent/message/edit'),
    'message__pin': ('POST', '/api/v1/agent/message/pin'),
    'message__react': ('POST', '/api/v1/agent/message/react'),
    'message__schedule': ('POST', '/api/v1/agent/message/schedule'),
    'campaign__post': ('POST', '/api/v1/agent/campaign/post'),
    'campaign__invite_boost': ('POST', '/api/v1/agent/campaign/invite-boost'),
    'campaign__referral_post': ('POST', '/api/v1/agent/campaign/referral-post'),
    'campaign__social_push': ('POST', '/api/v1/agent/campaign/social-push'),
    'campaign__growth_post': ('POST', '/api/v1/agent/campaign/growth-post'),
    'campaign__multi_channel': ('POST', '/api/v1/agent/campaign/multi-channel'),
    'role__add': ('POST', '/api/v1/agent/role/add'),
    'role__remove': ('POST', '/api/v1/agent/role/remove'),
    'role__list': ('GET', '/api/v1/agent/role/list'),
    'role__bulk_assign': ('POST', '/api/v1/agent/role/bulk-assign'),
    'member__kick': ('POST', '/api/v1/agent/member/kick'),
    'member__ban': ('POST', '/api/v1/agent/member/ban'),
    'member__unban': ('POST', '/api/v1/agent/member/unban'),
    'member__list': ('GET', '/api/v1/agent/member/list'),
    'member__search': ('GET', '/api/v1/agent/member/search'),
    'thread__create': ('POST', '/api/v1/agent/thread/create'),
    'invite__create': ('POST', '/api/v1/agent/invite/create'),
    'invite__list': ('GET', '/api/v1/agent/invite/list'),
    'invite__delete': ('POST', '/api/v1/agent/invite/delete'),
    'webhook__create': ('POST', '/api/v1/agent/webhook/create'),
    'webhook__send': ('POST', '/api/v1/agent/webhook/send'),
    'webhook__delete': ('DELETE', '/api/v1/agent/webhook/delete'),
    'webhook__list': ('GET', '/api/v1/agent/webhook/list'),
    'server__info': ('GET', '/api/v1/agent/server/info'),
    'server__channels': ('GET', '/api/v1/agent/server/channels'),
    'server__roles': ('GET', '/api/v1/agent/server/roles'),
}


class AIChatService:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def chat(
        self,
        message: str,
        conversation_history: list[dict] | None = None,
        system_prompt: str = 'You are a helpful Discord community assistant.',
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        api_key = self.settings.openrouter_api_key
        if not api_key:
            return {
                'response': '[AI service unavailable - no API key configured]',
                'model': self.settings.openrouter_model,
                'usage': {},
            }

        messages = [{'role': 'system', 'content': system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        # Keep request payloads bounded for provider compatibility and predictable token usage.
        messages.append({'role': 'user', 'content': message[:4000]})

        payload = {
            'model': self.settings.openrouter_model,
            'messages': messages,
            'max_tokens': max_tokens or self.settings.openrouter_max_tokens,
            'temperature': temperature if temperature is not None else self.settings.openrouter_temperature,
        }
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        timeout = self.settings.openrouter_response_timeout

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    'https://openrouter.ai/api/v1/chat/completions',
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                text = data['choices'][0]['message']['content']
                usage = data.get('usage', {})
                return {
                    'response': text,
                    'model': self.settings.openrouter_model,
                    'usage': usage,
                }
        except httpx.HTTPStatusError as exc:
            return {
                'response': f'[AI error: HTTP {exc.response.status_code}]',
                'model': self.settings.openrouter_model,
                'usage': {},
            }
        except httpx.HTTPError as exc:
            return {
                'response': f'[AI error: {type(exc).__name__}]',
                'model': self.settings.openrouter_model,
                'usage': {},
            }

    def _fallback_tool_schema(self) -> list[dict]:
        tools: list[dict] = []
        for tool_name, (_, route) in AGENT_TOOL_ROUTES.items():
            desc = f'Call {route} via {tool_name}'
            properties: dict[str, Any] = {'guild_id': {'type': 'string'}}
            required: list[str] = []
            if tool_name.startswith('channel__send_embed'):
                desc = 'Send a fully customized embed message to a Discord channel.'
                properties = {
                    'channel_id': {'type': 'string', 'description': 'Discord channel ID'},
                    'content': {'type': 'string'},
                    'title': {'type': 'string'},
                    'description': {'type': 'string'},
                    'color': {'type': 'integer'},
                    'fields': {'type': 'array', 'items': {'type': 'object'}},
                    'footer_text': {'type': 'string'},
                    'image_url': {'type': 'string'},
                    'thumbnail_url': {'type': 'string'},
                    'author_name': {'type': 'string'},
                    'mention_everyone': {'type': 'boolean', 'default': False},
                    'mention_roles': {'type': 'array', 'items': {'type': 'string'}},
                    'mention_users': {'type': 'array', 'items': {'type': 'string'}},
                }
                required = ['channel_id']
            tools.append(
                {
                    'type': 'function',
                    'function': {
                        'name': tool_name,
                        'description': desc,
                        'parameters': {
                            'type': 'object',
                            'properties': properties,
                            'required': required,
                            'additionalProperties': True,
                        },
                    },
                }
            )
        return tools

    async def _load_available_tools(self) -> list[dict]:
        local_base = os.getenv('DFA_LOCAL_API_BASE', 'http://127.0.0.1:8000')
        timeout = min(10, self.settings.openrouter_response_timeout)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f'{local_base}/api/v1/agent/tools/help')
                response.raise_for_status()
                payload = response.json()
                tools = payload.get('tools', [])
                schemas: list[dict] = []
                for item in tools:
                    name = item.get('name')
                    if not name:
                        continue
                    schemas.append(
                        {
                            'type': 'function',
                            'function': {
                                'name': name,
                                'description': item.get('description', ''),
                                'parameters': item.get('parameters') or {'type': 'object', 'additionalProperties': True},
                            },
                        }
                    )
                return schemas or self._fallback_tool_schema()
        except Exception:
            return self._fallback_tool_schema()

    async def _execute_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        route = AGENT_TOOL_ROUTES.get(name)
        if not route:
            return {'status': 'error', 'detail': f'Unknown tool: {name}'}
        method, path = route
        local_base = os.getenv('DFA_LOCAL_API_BASE', 'http://127.0.0.1:8000')
        timeout = min(30, self.settings.openrouter_response_timeout)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == 'GET':
                    response = await client.get(f'{local_base}{path}', params=arguments)
                elif method == 'PATCH':
                    response = await client.patch(f'{local_base}{path}', json=arguments)
                elif method == 'DELETE':
                    response = await client.request('DELETE', f'{local_base}{path}', json=arguments)
                else:
                    response = await client.post(f'{local_base}{path}', json=arguments)
                data = response.json() if response.content else {}
                if response.status_code >= 400:
                    return {
                        'status': 'error',
                        'http_status': response.status_code,
                        'detail': data.get('detail') if isinstance(data, dict) else str(data),
                        'data': data,
                    }
                return {'status': 'ok', 'http_status': response.status_code, 'data': data}
        except Exception as exc:
            return {'status': 'error', 'detail': str(exc)}

    async def agent_chat(
        self,
        message: str,
        conversation_history: list[dict] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        guild_id: str | None = None,
        available_tools: list[dict] | None = None,
        max_agent_steps: int = 30,
        db=None,
    ) -> dict:
        api_key = self.settings.openrouter_api_key
        if not api_key:
            return {
                'response': '[AI service unavailable - no API key configured]',
                'steps': [],
                'tool_calls_made': 0,
                'model': self.settings.openrouter_model,
                'usage': {},
            }

        tools = available_tools or await self._load_available_tools()
        prompt = system_prompt or 'You are a tool-using Discord operations assistant. Use tools whenever actions are requested.'
        messages: list[dict[str, Any]] = [{'role': 'system', 'content': prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        if guild_id:
            guild_context = str(guild_id).strip()[:128]
            messages.append({'role': 'system', 'content': f'Current guild context: {guild_context}'})
        # Keep request payloads bounded for provider compatibility and predictable token usage.
        messages.append({'role': 'user', 'content': message[:4000]})

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        timeout = self.settings.openrouter_response_timeout
        steps: list[dict] = []
        usage: dict[str, Any] = {}
        tool_calls_made = 0

        async with httpx.AsyncClient(timeout=timeout) as client:
            for step in range(1, max_agent_steps + 1):
                payload: dict[str, Any] = {
                    'model': self.settings.openrouter_model,
                    'messages': messages,
                    'tools': tools,
                    'tool_choice': 'auto',
                    'max_tokens': max_tokens or self.settings.openrouter_max_tokens,
                    'temperature': temperature if temperature is not None else self.settings.openrouter_temperature,
                }
                response = await client.post('https://openrouter.ai/api/v1/chat/completions', json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                usage = data.get('usage', usage)
                choice = data['choices'][0]['message']
                tool_calls = choice.get('tool_calls') or []

                if not tool_calls:
                    final_response = choice.get('content') or ''
                    return {
                        'response': final_response,
                        'steps': steps,
                        'tool_calls_made': tool_calls_made,
                        'model': data.get('model', self.settings.openrouter_model),
                        'usage': usage,
                    }

                messages.append(
                    {
                        'role': 'assistant',
                        'content': choice.get('content') or '',
                        'tool_calls': tool_calls,
                    }
                )

                for tool_call in tool_calls:
                    fn = tool_call.get('function') or {}
                    tool_name = fn.get('name')
                    raw_args = fn.get('arguments') or '{}'
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        args = {'_raw': raw_args}
                    if guild_id and 'guild_id' not in args:
                        args['guild_id'] = guild_id
                    result = await self._execute_tool_call(tool_name, args)
                    tool_calls_made += 1
                    steps.append(
                        {
                            'step': step,
                            'tool': tool_name,
                            'arguments': args,
                            'result': result,
                        }
                    )
                    messages.append(
                        {
                            'role': 'tool',
                            'tool_call_id': tool_call.get('id'),
                            'name': tool_name,
                            'content': json.dumps(result, ensure_ascii=False),
                        }
                    )

        return {
            'response': '[Agent stopped before final response]',
            'steps': steps,
            'tool_calls_made': tool_calls_made,
            'model': self.settings.openrouter_model,
            'usage': usage,
        }

"""Base entity helpers for the Mistral integration."""

from __future__ import annotations

import json
import re
import secrets
import string
from datetime import date, datetime, time
from typing import Any, AsyncIterator, Callable, Iterable, Dict

from voluptuous_openapi import convert

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity import Entity

from .const import (
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_REASONING_EFFORT,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    DEFAULT_NAME,
    LOGGER,
    MAX_TOOL_ITERATIONS,
    RECOMMENDED_CHAT_MODEL,
    CONF_DEFAULT_MEDIA_PLAYER,
    DEFAULT_VOICE_BOX,
    CONF_MUSIC_ASSISTANT_CONFIG_ENTRY,
    DEFAULT_MUSIC_ASSISTANT_CONFIG_ENTRY,
)
from .mistral_client import MistralClient

_MISTRAL_ID_RE = re.compile(r"^[A-Za-z0-9]{9}$")


def _json_default(obj: Any) -> Any:
    """JSON serializer fallback for types not natively supported."""
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


def _gen_mistral_id() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(9))


def _normalize_incoming_tool_id(mistral_id: str | None, id_map: Dict[str, str]) -> str:
    """Map incoming Mistral tool call IDs back to internal IDs."""
    if not mistral_id:
        mistral_id = _gen_mistral_id()
    for internal_id, external_id in id_map.items():
        if external_id == mistral_id:
            return internal_id
    id_map[mistral_id] = mistral_id
    return mistral_id


def _normalize_outgoing_tool_id(orig_id: str | None, id_map: Dict[str, str]) -> str:
    if orig_id is None: orig_id = ""
    if _MISTRAL_ID_RE.match(orig_id): return orig_id
    if orig_id in id_map: return id_map[orig_id]
    new_id = _gen_mistral_id()
    while new_id in id_map.values(): new_id = _gen_mistral_id()
    id_map[orig_id] = new_id
    return new_id


def _format_tool(tool: llm.Tool, serializer: Callable[[Any], Any] | None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": convert(
                tool.parameters,
                custom_serializer=serializer or llm.selector_serializer,
            ),
        },
    }


def _convert_chat_content(
    content: conversation.Content, id_map: Dict[str, str], *, extra_prompt: str | None
) -> list[dict]:
    """Convert ChatLog content to Mistral message objects."""
    if isinstance(content, conversation.UserContent):
        if content.attachments:
            raise HomeAssistantError("This provider does not support attachments yet")
        text = content.content or ""
        if extra_prompt:
            text = f"{text}\n\n{extra_prompt}"
        return [{"role": "user", "content": text}]

    if isinstance(content, conversation.ToolResultContent):
        return [{
            "role": "tool",
            "tool_call_id": _normalize_outgoing_tool_id(content.tool_call_id, id_map),
            "name": content.tool_name,
            "content": json.dumps(content.tool_result, default=_json_default, ensure_ascii=False),
        }]
    if isinstance(content, conversation.AssistantContent):
        msg: dict[str, Any] = {"role": "assistant"}
        if content.content: msg["content"] = content.content
        if content.tool_calls:
            msg["tool_calls"] = [{
                "id": _normalize_outgoing_tool_id(tc.id, id_map),
                "type": "function",
                "function": {
                    "name": tc.tool_name,
                    "arguments": json.dumps(tc.tool_args, default=_json_default, ensure_ascii=False),
                },
            } for tc in content.tool_calls]
        return [msg]
    return [{"role": content.role, "content": content.content}] if content.content else []


def _build_messages(
    chat_content: Iterable[conversation.Content],
    id_map: Dict[str, str],
    extra_prompt: str | None,
) -> list[dict]:
    messages: list[dict] = []
    for content in chat_content:
        messages.extend(_convert_chat_content(content, id_map, extra_prompt=extra_prompt if content.role == "user" else None))
    return messages

async def _transform_stream(
    stream: AsyncIterator[dict[str, Any]],
    id_map: Dict[str, str],
    external_calls: list[tuple[str, str, dict[str, Any]]],
) -> AsyncIterator[conversation.AssistantContentDeltaDict]:
    """Transform Mistral SSE stream into Home Assistant deltas."""
    tool_call_buffers: dict[int, dict[str, Any]] = {}
    async for chunk in stream:
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            if delta.get("content"):
                yield conversation.AssistantContentDeltaDict(content=delta["content"])
            if delta.get("tool_calls"):
                for tc in delta["tool_calls"]:
                    idx = tc.get("index", 0)
                    buf = tool_call_buffers.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.get("id"):
                        buf["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        buf["name"] = fn["name"]
                    if fn.get("arguments"):
                        buf["arguments"] += fn["arguments"]

            finish_reason = choice.get("finish_reason")
            if finish_reason in ("tool_calls", "stop"):
                if tool_call_buffers:
                    inputs: list[llm.ToolInput] = []
                    for idx in sorted(tool_call_buffers):
                        buf = tool_call_buffers[idx]
                        try:
                            args = json.loads(buf["arguments"]) if buf["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {}
                        internal_id = _normalize_incoming_tool_id(buf["id"], id_map)
                        external = buf["name"].startswith("music_assistant.")
                        inputs.append(
                            llm.ToolInput(
                                tool_name=buf["name"],
                                tool_args=args,
                                id=internal_id,
                                external=external,
                            )
                        )
                        if external:
                            external_calls.append((internal_id, buf["name"], args))
                    tool_call_buffers.clear()
                    yield conversation.AssistantContentDeltaDict(tool_calls=inputs)

                yield conversation.AssistantContentDeltaDict(role="assistant")

    if tool_call_buffers:
        inputs = []
        for idx in sorted(tool_call_buffers):
            buf = tool_call_buffers[idx]
            try:
                args = json.loads(buf["arguments"]) if buf["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            internal_id = _normalize_incoming_tool_id(buf["id"], id_map)
            external = buf["name"].startswith("music_assistant.")
            inputs.append(
                llm.ToolInput(
                    tool_name=buf["name"],
                    tool_args=args,
                    id=internal_id,
                    external=external,
                )
            )
            if external:
                external_calls.append((internal_id, buf["name"], args))
        yield conversation.AssistantContentDeltaDict(tool_calls=inputs)
    yield conversation.AssistantContentDeltaDict(role="assistant")

class MistralBaseLLMEntity(Entity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(entry.domain, subentry.subentry_id)},
            name=entry.title or DEFAULT_NAME,
            manufacturer="Mistral AI",
            model=subentry.data.get(CONF_CHAT_MODEL, RECOMMENDED_CHAT_MODEL),
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    def _get_music_assistant_config_entry_id(self, hass: HomeAssistant) -> str | None:
        """Get the config_entry_id for Music Assistant integration."""
        # Einfach direkt nutzen - kein extra import nötig!
        return self._options().get(
            CONF_MUSIC_ASSISTANT_CONFIG_ENTRY, 
            DEFAULT_MUSIC_ASSISTANT_CONFIG_ENTRY
        )

    @property
    def client(self) -> MistralClient:
        return self.entry.runtime_data

    def _options(self):
        return self.subentry.data or {}

    async def _async_handle_chat_log(
        self,
        hass: HomeAssistant,
        chat_log: conversation.ChatLog,
        structure_prompt: str | None = None,
        use_streaming: bool = True,
    ) -> conversation.AssistantContent:
        """Handle chat log and process tool calls."""
        options = self._options()
        id_map: Dict[str, str] = {}
        model = options.get(CONF_CHAT_MODEL, RECOMMENDED_CHAT_MODEL)

        default_player = options.get(CONF_DEFAULT_MEDIA_PLAYER, DEFAULT_VOICE_BOX)
        default_player_name = (
            hass.states.get(default_player).name
            if default_player and hass.states.get(default_player)
            else default_player
        )
        default_instruction = None
        if default_player:
            target_name = default_player_name or default_player
            default_instruction = (
                "When asked to play music, prefer using the configured media player "
                f"`{target_name}` ({default_player})."
            )

        extra_system_prompt: str | None = None
        if structure_prompt:
            extra_system_prompt = structure_prompt
        if default_instruction:
            extra_system_prompt = (
                f"{default_instruction}"
                if not extra_system_prompt
                else f"{extra_system_prompt}\n\n{default_instruction}"
            )

        def _inject_extra_system_message(messages: list[dict]) -> None:
            if not extra_system_prompt:
                return
            if messages and messages[0].get("role") == "system" and messages[0].get("content"):
                messages[0]["content"] = f"{messages[0]['content']}\n\n{extra_system_prompt}"
            else:
                messages.insert(0, {"role": "system", "content": extra_system_prompt})

        for _ in range(MAX_TOOL_ITERATIONS):
            messages = _build_messages(chat_log.content, id_map, extra_prompt=None)
            _inject_extra_system_message(messages)

            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": use_streaming,
            }

            if options.get(CONF_MAX_TOKENS) is not None:
                payload["max_tokens"] = options.get(CONF_MAX_TOKENS)
            if options.get(CONF_TEMPERATURE) is not None:
                payload["temperature"] = options.get(CONF_TEMPERATURE)
            if options.get(CONF_TOP_P) is not None:
                payload["top_p"] = options.get(CONF_TOP_P)
            if options.get(CONF_REASONING_EFFORT):
                payload["reasoning_effort"] = options.get(CONF_REASONING_EFFORT)
            if structure_prompt:
                payload["response_format"] = {"type": "json_object"}

            tools: list[dict[str, Any]] = []
            llm_tools = chat_log.llm_api.tools if chat_log.llm_api else []
            if llm_tools:
                tools.extend(
                    _format_tool(tool, chat_log.llm_api.custom_serializer)
                    for tool in llm_tools
                )

            # Provide basic Music Assistant tools if none are supplied by the LLM API.
            if default_player and not any(
                tool.name.startswith("music_assistant.") for tool in llm_tools
            ):
                tools.extend(
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": "music_assistant.search",
                                "description": "Search for music in Music Assistant.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "name": {
                                            "type": "string",
                                            "description": "Query term to search for.",
                                        },
                                        "limit": {
                                            "type": "integer",
                                            "description": "How many results to return.",
                                            "default": 1,
                                        },
                                        "artist": {
                                            "type": "string",
                                            "description": "Artist filter.",
                                        },
                                        "media_type": {
                                            "type": "string",
                                            "description": "media type such as track or album.",
                                        },
                                    },
                                    "required": ["name"],
                                },
                            },
                        },
                        {
                            "type": "function",
                            "function": {
                                "name": "music_assistant.play_media",
                                "description": "Start playback via Music Assistant.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "entity_id": {
                                            "type": "string",
                                            "description": "Media player entity to target.",
                                        },
                                        "media_id": {
                                            "type": "string",
                                            "description": "Identifier from search results.",
                                        },
                                        "media_type": {
                                            "type": "string",
                                            "description": "track, album, playlist…",
                                        },
                                    },
                                    "required": ["media_id", "media_type"],
                                },
                            },
                        },
                    ]
                )

            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            external_calls: list[tuple[str, str, dict[str, Any]]] = []
            if use_streaming:
                stream = self.client.chat_stream(payload)
                async for _ in chat_log.async_add_delta_content_stream(
                    self.entity_id or self.unique_id,
                    _transform_stream(stream, id_map, external_calls),
                ):
                    pass
            else:
                response = await self.client.chat(payload)
                await self._async_process_response(
                    chat_log, response, id_map, external_calls
                )

            if external_calls:
                await self._async_handle_external_tools(
                    hass, chat_log, external_calls, default_player
                )

            if not chat_log.unresponded_tool_results:
                break
        else:
            LOGGER.warning("Max tool iterations reached")
            raise HomeAssistantError("Too many tool iterations for Mistral response")

        last_assistant = next(
            (
                content
                for content in reversed(chat_log.content)
                if isinstance(content, conversation.AssistantContent)
            ),
            None,
        )
        if not last_assistant:
            raise HomeAssistantError("Mistral did not return a response")
        return last_assistant

    async def _async_process_response(
        self,
        chat_log: conversation.ChatLog,
        response: dict[str, Any],
        id_map: Dict[str, str],
        external_calls: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """Process a non-streaming Mistral response and feed it to the chat log."""
        async def _delta_iter() -> AsyncIterator[conversation.AssistantContentDeltaDict]:
            usage = response.get("usage")
            if usage:
                chat_log.async_trace(
                    {
                        "stats": {
                            "input_tokens": usage.get("prompt_tokens", 0),
                            "output_tokens": usage.get("completion_tokens", 0),
                        }
                    }
                )

            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            if message.get("content"):
                yield conversation.AssistantContentDeltaDict(content=message["content"])

            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                inputs: list[llm.ToolInput] = []
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    internal_id = _normalize_incoming_tool_id(tc.get("id"), id_map)
                    external = name.startswith("music_assistant.")
                    inputs.append(
                        llm.ToolInput(
                            tool_name=name,
                            tool_args=args,
                            id=internal_id,
                            external=external,
                        )
                    )
                    if external:
                        external_calls.append((internal_id, name, args))
                yield conversation.AssistantContentDeltaDict(tool_calls=inputs)

            yield conversation.AssistantContentDeltaDict(role="assistant")

        async for _ in chat_log.async_add_delta_content_stream(
            self.entity_id or self.unique_id, _delta_iter()
        ):
            pass

    async def _async_handle_external_tools(
        self,
        hass: HomeAssistant,
        chat_log: conversation.ChatLog,
        external_calls: list[tuple[str, str, dict[str, Any]]],
        default_player: str | None,
    ) -> None:
        """Handle external tool calls (currently Music Assistant helpers)."""
        ma_config_entry_id = self._get_music_assistant_config_entry_id(hass)
        for call_id, tool_name, tool_args in external_calls:
            result: dict[str, Any]
            try:
                domain, _, service = tool_name.partition(".")
                if domain != "music_assistant":
                    raise HomeAssistantError(f"Unsupported external tool {tool_name}")

                args = dict(tool_args)
                target_entity = args.pop("entity_id", default_player)

                if service == "play_media":
                    filtered = {k: v for k, v in args.items() if k in {"media_id", "media_type"}}
                    await hass.services.async_call(
                        "music_assistant",
                        "play_media",
                        filtered,
                        blocking=True,
                        target={"entity_id": target_entity} if target_entity else None,
                    )
                    result = {"status": "success", "message": "Playback started"}
                elif service in {"search", "get_library"}:
                    allowed = {"name", "limit", "media_type", "artist", "offset", "order_by"}
                    if ma_config_entry_id:
                        args["config_entry_id"] = ma_config_entry_id
                        allowed.add("config_entry_id")
                    filtered = {k: v for k, v in args.items() if k in allowed}
                    response = await hass.services.async_call(
                        "music_assistant",
                        service,
                        filtered,
                        blocking=True,
                        return_response=True,
                    )
                    result = response or {"status": "success"}
                else:
                    raise HomeAssistantError(f"Unsupported Music Assistant tool {tool_name}")
            except Exception as err:  # pylint: disable=broad-except
                LOGGER.error("External tool '%s' failed: %s", tool_name, err)
                result = {"error": str(err)}

            chat_log.async_add_assistant_content_without_tools(
                conversation.ToolResultContent(
                    agent_id=self.entity_id or self.unique_id,
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    tool_result=result,
                )
            )

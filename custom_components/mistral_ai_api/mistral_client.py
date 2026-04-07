# Modified by Louis Rokitta
"""Mistral API client helpers."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict

import httpx

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_MODELS_URL = "https://api.mistral.ai/v1/models"
LOGGER = logging.getLogger(__name__)


class MistralClient:
    """Wrapper around Mistral's chat endpoint."""

    def __init__(self, api_key: str, http_client: httpx.AsyncClient) -> None:
        if not api_key:
            raise ValueError("API key required for Mistral client")
        if http_client is None:
            raise ValueError("HTTP client required for Mistral client")
        self.api_key = api_key
        self.http_client = http_client

    async def validate_api_key(self) -> None:
        """Validate the API key by listing models."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        response = await self.http_client.get(
            MISTRAL_MODELS_URL,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()

    async def chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send a chat payload to Mistral."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = await self.http_client.post(
            MISTRAL_API_URL,
            json=payload,
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def chat_stream(self, payload: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """Send a chat payload to Mistral and stream the response.
        
        Returns an async iterator that yields response chunks from the Mistral API.
        Usage: async for chunk in client.chat_stream(payload): ...
        """
        async def _stream() -> AsyncIterator[Dict[str, Any]]:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            async with self.http_client.stream(
                "POST",
                MISTRAL_API_URL,
                json=payload,
                headers=headers,
                timeout=60,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data = line[6:]  # Remove "data: " prefix
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            yield chunk
                        except json.JSONDecodeError:
                            LOGGER.warning("Failed to parse SSE chunk: %s", data)
                            continue
        return _stream()

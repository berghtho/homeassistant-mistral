# GitHub Copilot Instructions for homeassistant-mistral

## Overview

This repository contains a Home Assistant custom integration that connects Home Assistant to the [Mistral AI](https://mistral.ai/) API. The integration domain is `mistral_ai_api` and lives under `custom_components/mistral_ai_api/`.

---

## Reference Documentation

### Primary Reference – Mistral AI

Always consult the **official Mistral AI documentation** as the authoritative source for:

- Available models and their capabilities: <https://docs.mistral.ai/getting-started/models/overview/>
- Chat Completions API (messages, roles, tool calls, streaming): <https://docs.mistral.ai/api/#tag/chat>
- Function / tool calling: <https://docs.mistral.ai/capabilities/function_calling/>
- Structured output / JSON mode: <https://docs.mistral.ai/capabilities/structured-output/>
- Reasoning / `reasoning_effort`: <https://docs.mistral.ai/capabilities/reasoning/>
- Embeddings, agents, and other capabilities: <https://docs.mistral.ai/>

When the Mistral docs conflict with assumptions based on other providers (OpenAI, Gemini, etc.), **the Mistral docs take precedence**.

### Secondary Reference – Home Assistant Developer Docs

Always consult the **Home Assistant developer documentation** for integration architecture, config flow patterns, entity platforms, service schemas, and LLM helpers:

- Developer docs: <https://developers.home-assistant.io/>
- Integration quality scale: <https://developers.home-assistant.io/docs/integration_quality_scale/rules/>
- Config flow: <https://developers.home-assistant.io/docs/config_entries_config_flow_handler/>
- LLM API helpers: <https://developers.home-assistant.io/docs/llm_api/>

---

## Blueprint Integrations

When implementing new features, fixing bugs, or refactoring, **always use the following official Home Assistant integrations as architectural blueprints** and follow their patterns closely:

| Integration | Location in HA core |
|---|---|
| OpenAI Conversation | `homeassistant/components/openai_conversation/` |
| Google Generative AI (Gemini) | `homeassistant/components/google_generative_ai_conversation/` |
| Anthropic Conversation | `homeassistant/components/anthropic/` |

Browse these integrations in the [Home Assistant core repository](https://github.com/home-assistant/core/tree/dev/homeassistant/components) to understand:

- How config entries and subentries are structured
- How `ConversationEntity` and `AITaskEntity` are implemented
- How LLM tools are serialised and tool-call responses are processed
- How streaming is handled (SSE / async generators)
- How options flows and schema validation are implemented
- How tests are structured (see `tests/components/<integration>/`)

When in doubt, match the patterns used by these integrations rather than inventing new ones.

---

## Key Conventions for This Integration

### File Structure

```
custom_components/mistral_ai_api/
├── __init__.py          # Entry setup, subentry handling, service registration
├── ai_task.py           # AITaskEntity – structured JSON output via Mistral
├── config_flow.py       # ConfigFlow + subentry options flows
├── const.py             # All constants, model lists, recommended defaults
├── conversation.py      # ConversationEntity – voice/chat agent
├── entity.py            # Shared base entity (MistralConversationEntity)
├── mistral_client.py    # Thin async HTTP wrapper around the Mistral REST API
├── services.yaml        # Service schema for mistral_ai_api.generate_content
├── strings.json         # UI strings (config flow labels, errors, etc.)
└── manifest.json        # Integration manifest
```

### Coding Standards

- Follow PEP 8 and the [Home Assistant code style](https://developers.home-assistant.io/docs/development_guidelines/).
- Use `async`/`await` throughout – no blocking I/O in the event loop.
- Use `httpx` (already declared as a dependency) for all HTTP calls to the Mistral API.
- Log via `LOGGER` (defined in `const.py`) at the appropriate level (`debug` for normal flow, `warning`/`error` for actionable issues).
- Keep API-specific logic in `mistral_client.py`; keep HA-specific logic in the entity files.
- Raise `HomeAssistantError` (or its subclasses) for user-visible errors; do not let raw exceptions propagate to the UI.

### Model and Parameter Handling

- The list of available models is maintained in `const.py` (`CHAT_MODELS`). Update this list when Mistral releases or deprecates models (verify against the official models page).
- `reasoning_effort` is only meaningful for models that support reasoning – check the Mistral docs before applying it.
- When "Recommended" settings are active, use the constants prefixed `RECOMMENDED_` from `const.py`.

### Tool Calls

- Home Assistant LLM tools must be converted to Mistral's function-calling schema before being sent to the API.
- Tool call IDs must be normalised to the Mistral-safe 9-character alphanumeric format.
- Follow the tool-call loop pattern used in the OpenAI and Anthropic integrations (bounded iteration, `MAX_TOOL_ITERATIONS`).

### Tests

- Mirror the test structure of the OpenAI / Gemini / Anthropic integration tests in HA core.
- Mock the Mistral HTTP client (`mistral_client.py`) rather than making real network calls.
- Cover config flow, entity setup, conversation turns, tool calls, error paths (auth failure, rate limits, timeouts), and streaming.

---

## Do Not

- Do not add image-generation features – the Mistral API does not provide an image generation endpoint.
- Do not introduce synchronous/blocking network calls in the async event loop.
- Do not hard-code model names outside of `const.py`.
- Do not bypass the `allowlist_external_dirs` check when reading local files.

"""Stdio MCP server boilerplate.

Run with:
    python server.py

Communicates over stdio using the Model Context Protocol (MCP).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IMAGE_MODELS_URI = "image-models://list"
IMAGE_MODELS_URL = "https://api.airforce/v1/models"
IMAGE_MODELS_TTL_SECONDS = 5 * 60  # cache for 5 minutes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# IMPORTANT: MCP stdio servers must not write to stdout — that channel is
# reserved for JSON-RPC frames. Send all logs to stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=__import__("sys").stderr,
)
log = logging.getLogger("mcp-server")

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
app = Server("boilerplate-mcp-server")


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Return the input text unchanged. Useful as a sanity check.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to echo back.",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="add",
            description="Add two numbers and return the sum.",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "First number."},
                    "b": {"type": "number", "description": "Second number."},
                },
                "required": ["a", "b"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------
@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    log.info("Tool call: %s args=%s", name, arguments)

    if name == "echo":
        text = arguments.get("text", "")
        if not isinstance(text, str):
            raise ValueError("'text' must be a string")
        return [TextContent(type="text", text=text)]

    if name == "add":
        a = arguments.get("a")
        b = arguments.get("b")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            raise ValueError("'a' and 'b' must be numbers")
        return [TextContent(type="text", text=str(a + b))]

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
_image_models_cache: list[str] | None = None
_image_models_cache_at: float = 0.0
_image_models_lock = asyncio.Lock()


async def _fetch_image_models(client: httpx.AsyncClient) -> list[str]:
    """Fetch the list of image model IDs from the airforce API."""
    response = await client.get(IMAGE_MODELS_URL, timeout=30.0)
    response.raise_for_status()
    payload = response.json()
    return [
        model["id"]
        for model in payload.get("data", [])
        if isinstance(model, dict) and model.get("media_type") == "image"
    ]


async def get_image_models(force_refresh: bool = False) -> list[str]:
    """Return cached image model IDs, refreshing when stale."""
    global _image_models_cache, _image_models_cache_at

    now = time.monotonic()
    if (
        not force_refresh
        and _image_models_cache is not None
        and (now - _image_models_cache_at) < IMAGE_MODELS_TTL_SECONDS
    ):
        return _image_models_cache

    async with _image_models_lock:
        # Re-check inside the lock to avoid duplicate refreshes.
        now = time.monotonic()
        if (
            not force_refresh
            and _image_models_cache is not None
            and (now - _image_models_cache_at) < IMAGE_MODELS_TTL_SECONDS
        ):
            return _image_models_cache

        log.info("Fetching image model list from %s", IMAGE_MODELS_URL)
        async with httpx.AsyncClient() as client:
            _image_models_cache = await _fetch_image_models(client)
            _image_models_cache_at = time.monotonic()
        log.info("Cached %d image models", len(_image_models_cache))
        return _image_models_cache


@app.list_resources()
async def list_resources() -> list[Resource]:
    return [
        Resource(
            uri=IMAGE_MODELS_URI,
            name="Image Models",
            description=(
                "List of image-generation model IDs available on "
                "api.airforce (filtered to media_type == 'image')."
            ),
            mimeType="application/json",
        ),
    ]


@app.read_resource()
async def read_resource(uri: str) -> str:
    if str(uri) == IMAGE_MODELS_URI:
        models = await get_image_models()
        return json.dumps({"models": models, "count": len(models)}, indent=2)
    raise ValueError(f"Unknown resource: {uri}")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="summarize",
            description="Ask the model to summarize a piece of text.",
            arguments=[
                PromptArgument(
                    name="text",
                    description="Text to summarize.",
                    required=True,
                ),
            ],
        ),
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict[str, Any] | None) -> GetPromptResult:
    arguments = arguments or {}
    if name == "summarize":
        text = arguments.get("text", "")
        return GetPromptResult(
            description="Summarize the provided text.",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"Please summarize the following text:\n\n{text}",
                    ),
                ),
            ],
        )
    raise ValueError(f"Unknown prompt: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    log.info("Starting MCP stdio server 'boilerplate-mcp-server'")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server stopped by user")

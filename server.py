"""Stdio MCP server boilerplate.

Run with:
    python server.py

Communicates over stdio using the Model Context Protocol (MCP).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
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
# Environment / configuration
# ---------------------------------------------------------------------------
# Best-effort .env loading so the server picks up AIRFORCE_API_KEY from a
# local .env file during development. python-dotenv is optional; if it isn't
# installed we fall back to plain os.environ (env vars set by the parent
# process / MCP host still work).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

AIRFORCE_API_KEY = os.environ.get("AIRFORCE_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IMAGE_MODELS_URI = "image-models://list"
IMAGE_MODELS_URL = "https://api.airforce/v1/models"
IMAGE_MODELS_TTL_SECONDS = 5 * 60  # cache for 5 minutes

IMAGE_GENERATIONS_URL = "https://api.airforce/v1/images/generations"
IMAGE_GENERATION_TIMEOUT_SECONDS = 300.0  # upstream renders can be slow

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# IMPORTANT: MCP stdio servers must not write to stdout — that channel is
# reserved for JSON-RPC frames. Send all logs to stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
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
        Tool(
            name="generate_image",
            description=(
                "Generate a single HD image from a text prompt using "
                "api.airforce. Returns the upstream JSON with a base64-encoded "
                "image in data[0].b64_json. Use the 'image-models://list' "
                "resource to discover available model IDs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": (
                            "Image model ID. Filter the 'image-models://list' "
                            "resource by media_type == 'image' to find valid "
                            "options (e.g. 'flux-2-dev', 'nano-banana-pro')."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Text description of the image to generate.",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": (
                            "Aspect ratio of the output: '16:9', '1:1', or "
                            "'9:16'. Validated against the model's "
                            "image_caps.aspect_ratios."
                        ),
                    },
                },
                "required": ["model", "prompt"],
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

    if name == "generate_image":
        return await _handle_generate_image(arguments)

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------
# Fields we forward to the upstream. n, quality, and response_format are
# hardcoded — callers control them through tool behavior, not arguments.
_GENERATE_IMAGE_FIXED = {
    "n": 1,
    "quality": "hd",
    "response_format": "b64_json",
}


async def _handle_generate_image(arguments: dict[str, Any]) -> list[TextContent]:
    """Call POST /v1/images/generations and return the upstream JSON."""
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")

    if not AIRFORCE_API_KEY:
        raise ValueError(
            "AIRFORCE_API_KEY is not configured. Set it in your environment "
            "or .env file before calling generate_image."
        )

    model = arguments.get("model")
    prompt = arguments.get("prompt")
    if not isinstance(model, str) or not model:
        raise ValueError("'model' is required and must be a non-empty string")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("'prompt' is required and must be a non-empty string")

    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        **_GENERATE_IMAGE_FIXED,
    }

    aspect_ratio = arguments.get("aspect_ratio")
    if aspect_ratio is not None:
        if not isinstance(aspect_ratio, str):
            raise ValueError("'aspect_ratio' must be a string")
        body["aspect_ratio"] = aspect_ratio

    log.info(
        "generate_image: model=%s prompt_len=%d aspect_ratio=%s",
        model,
        len(prompt),
        body.get("aspect_ratio"),
    )

    async with _build_http_client() as client:
        try:
            response = await client.post(
                IMAGE_GENERATIONS_URL,
                json=body,
                timeout=IMAGE_GENERATION_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            log.exception("generate_image: HTTP error talking to upstream")
            raise ValueError(f"Failed to reach image generation API: {exc}") from exc

    if response.status_code >= 400:
        # Pass the upstream error body through verbatim so the model can
        # diagnose validation/capability issues.
        detail = response.text
        try:
            detail_json = response.json()
            detail = json.dumps(detail_json)
        except ValueError:
            pass
        log.error(
            "generate_image: upstream %s: %s", response.status_code, detail
        )
        raise ValueError(
            f"Image generation API returned HTTP {response.status_code}: {detail}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(
            "Image generation API returned a non-JSON response."
        ) from exc

    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


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


def _build_http_client() -> httpx.AsyncClient:
    """Build an httpx client, attaching the AirForce API key if configured."""
    headers: dict[str, str] = {"Accept": "application/json"}
    if AIRFORCE_API_KEY:
        headers["Authorization"] = f"Bearer {AIRFORCE_API_KEY}"
    else:
        log.warning(
            "AIRFORCE_API_KEY is not set; authenticated endpoints will fail. "
            "Set it in your environment or .env file (see .env.example)."
        )
    return httpx.AsyncClient(headers=headers)


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
        async with _build_http_client() as client:
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

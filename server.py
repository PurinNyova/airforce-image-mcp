"""Stdio MCP Image Generation Server.

Run with:
    python server.py

Communicates over stdio using the Model Context Protocol (MCP).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
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
    Resource,
    TextContent,
    Tool,
)

# Best-effort .env loading for local dev.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

AIRFORCE_API_KEY = os.environ.get("AIRFORCE_API_KEY", "").strip()

IMAGE_MODELS_URI = "image-models://list"
IMAGE_MODELS_URL = "https://api.airforce/v1/models"
IMAGE_MODELS_TTL = 5 * 60  # seconds

IMAGE_GENERATIONS_URL = "https://api.airforce/v1/images/generations"
IMAGE_GENERATION_TIMEOUT = 300.0

# MCP stdio servers must not write to stdout — that's reserved for JSON-RPC.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp-server")

app = Server("image-generation-mcp")


def _http_client() -> httpx.AsyncClient:
    headers = {"Accept": "application/json"}
    if AIRFORCE_API_KEY:
        headers["Authorization"] = f"Bearer {AIRFORCE_API_KEY}"
    return httpx.AsyncClient(headers=headers)


# ---------------------------------------------------------------------------
# Image format detection
# ---------------------------------------------------------------------------
# Sniffed from the decoded base64 bytes. The upstream may return JPEG, PNG,
# WebP, GIF, or BMP depending on the model — we MUST match the actual bytes,
# not hardcode jpeg, or the data URL will misrepresent the payload.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),  # disambiguated by 'WEBP' at offset 8 below
    (b"BM", "bmp"),
)


def _detect_image_format(raw: bytes) -> str:
    """Return an image MIME subtype for ``raw`` based on its magic bytes.

    Falls back to ``"jpeg"`` when the input is too short or doesn't match
    any known signature, since api.airforce's most common return is JPEG.
    """
    for prefix, fmt in _IMAGE_MAGIC:
        if raw.startswith(prefix):
            if fmt == "webp" and (len(raw) < 12 or raw[8:12] != b"WEBP"):
                continue
            return fmt
    return "jpeg"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
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
                            "Image model ID (e.g. 'flux-2-dev', 'nano-banana-pro'). "
                            "See the 'image-models://list' resource."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Text description of the image to generate.",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "Aspect ratio: '16:9', '1:1', or '9:16'.",
                    },
                },
                "required": ["model", "prompt"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    log.info("Tool call: %s", name)

    if name == "generate_image":
        return await _generate_image(arguments)

    raise ValueError(f"Unknown tool: {name}")


async def _generate_image(arguments: dict[str, Any]) -> list[TextContent]:
    if not AIRFORCE_API_KEY:
        raise ValueError("AIRFORCE_API_KEY is not configured.")

    body = {
        "model": arguments["model"],
        "prompt": arguments["prompt"],
        "n": 1,
        "response_format": "b64_json",
        "sse": False,
    }
    if "aspect_ratio" in arguments:
        body["aspect_ratio"] = arguments["aspect_ratio"]

    log.info("generate_image: model=%s", body["model"])

    async with _http_client() as client:
        response = await client.post(
            IMAGE_GENERATIONS_URL, json=body, timeout=IMAGE_GENERATION_TIMEOUT
        )

    if response.status_code >= 400:
        raise ValueError(
            f"Image generation API returned HTTP {response.status_code}: {response.text}"
        )

    payload = response.json()

    # Wrap b64 strings as data URLs so callers can use them directly. We
    # detect the actual image format from the decoded magic bytes — the
    # upstream may return jpeg, png, webp, gif, or bmp depending on model.
    for item in payload.get("data", []):
        b64 = item.get("b64_json") if isinstance(item, dict) else None
        if not isinstance(b64, str) or not b64:
            continue
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            # Leave undecodable payloads untouched — the caller will see
            # the raw base64 string and can diagnose upstream issues.
            log.warning("generate_image: non-base64 payload from upstream, skipping wrap")
            continue
        fmt = _detect_image_format(raw)
        item["b64_json"] = f"data:image/{fmt};base64,{b64}"

    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
# Simple TTL cache. asyncio is single-threaded; a duplicate fetch on cold
# start is harmless, so no lock needed.
_models_cache: tuple[float, list[str]] | None = None


async def get_image_models() -> list[str]:
    global _models_cache

    if _models_cache and (time.monotonic() - _models_cache[0]) < IMAGE_MODELS_TTL:
        return _models_cache[1]

    log.info("Fetching image model list")
    async with _http_client() as client:
        response = await client.get(IMAGE_MODELS_URL, timeout=30.0)
        response.raise_for_status()
        payload = response.json()

    models = [
        m["id"]
        for m in payload.get("data", [])
        if isinstance(m, dict) and m.get("media_type") == "image"
    ]
    _models_cache = (time.monotonic(), models)
    return models


@app.list_resources()
async def list_resources() -> list[Resource]:
    return [
        Resource(
            uri=IMAGE_MODELS_URI,
            name="Image Models",
            description="List of image-generation model IDs available on api.airforce.",
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
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    log.info("Starting MCP stdio server 'image-generation-mcp-server'")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server stopped by user")
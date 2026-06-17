# Image-Generation-MCP

An MCP (Model Context Protocol) stdio server that generates images from text
prompts via [api.airforce](https://api.airforce). It exposes a single
`generate_image` tool, an `image-models://list` resource for model discovery,
and runs over stdio so it works with any MCP-aware agent (Claude Desktop,
Claude Code, Cursor, Cline, Hermes, etc.).

---

## Features

- **`generate_image` tool** — text-to-image via the OpenAI-compatible
  `/v1/images/generations` endpoint.
- **Model discovery resource** — `image-models://list` returns the live list
  of image-capable model IDs from `/v1/models`, cached for 5 minutes.
- **Format-aware data URLs** — `data[0].b64_json` is wrapped as
  `data:image/<fmt>;base64,...` with the actual format (jpeg/png/webp/gif/bmp)
  sniffed from the payload, not hardcoded.
- **Zero-config transport** — stdio only, no HTTP port to manage.

---

## One-click install

This repo ships a `.mcp.json` at the root. Most MCP clients (Claude Code,
Cursor, Cline, Continue, Hermes) auto-discover it.

### 1. Set the API key

Get a key at <https://api.airforce/> and export it:

```bash
# Linux / macOS
export AIRFORCE_API_KEY="sk-air-..."

# Windows PowerShell
$env:AIRFORCE_API_KEY = "sk-air-..."
```

Or copy `.env.example` to `.env` and fill it in — the server loads `.env`
automatically when `python-dotenv` is installed (it is, as a dependency).

### 2. Run

The package is published on PyPI as `image-generation-mcp`. No manual
install needed — the client invokes it via `uvx`:

```bash
uvx image-generation-mcp
```

`uv` will create an ephemeral venv, install the package, and run the server's
console entry point on first use.

---

## Client configuration

### Claude Desktop

Add to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`,
Windows: `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "image-generation": {
      "command": "uvx",
      "args": ["image-generation-mcp"],
      "env": {
        "AIRFORCE_API_KEY": "sk-air-..."
      }
    }
  }
}
```

### Claude Code / Cursor / Cline (project-level)

A `.mcp.json` is already provided at the repo root:

```json
{
  "mcpServers": {
    "image-generation": {
      "command": "uvx",
      "args": ["image-generation-mcp"],
      "env": { "AIRFORCE_API_KEY": "${env:AIRFORCE_API_KEY}" }
    }
  }
}
```

The `${env:AIRFORCE_API_KEY}` syntax delegates to the parent shell — set the
env var once in your shell rc and every project picks it up.

### Hermes / generic MCP client

Hermes and other stdio-MCP clients need the same shape: a command, args, and
optional env. Point them at `uvx` with `image-generation-mcp` as the only
arg, and pass `AIRFORCE_API_KEY` in the env block.

### Local development (run from a checkout)

When hacking on the server itself, run it from the repo root:

```json
{
  "mcpServers": {
    "image-generation": {
      "command": "uv",
      "args": ["run", "--project", ".", "image-generation-mcp"],
      "env": { "AIRFORCE_API_KEY": "sk-air-..." }
    }
  }
}
```

This uses the local `pyproject.toml` instead of the published package, so
edits to `server.py` take effect on the next client restart.

---

## Environment variables

| Variable            | Required | Description                                                 |
| ------------------- | -------- | ----------------------------------------------------------- |
| `AIRFORCE_API_KEY`  | Yes      | Bearer token for api.airforce. Get one at <https://api.airforce/>. |

If the key is missing, `generate_image` raises a clear error at call time
instead of failing deep inside the HTTP layer.

---

## Tools

### `generate_image`

Generate a single image from a text prompt.

**Arguments**

| Name           | Type   | Required | Description                                                  |
| -------------- | ------ | -------- | ------------------------------------------------------------ |
| `model`        | string | Yes      | Image model ID, e.g. `flux-2-dev`, `nano-banana-pro`.        |
| `prompt`       | string | Yes      | Text description of the desired image.                       |
| `aspect_ratio` | string | No       | `1:1`, `16:9`, or `9:16`. Some models support more — see `image-models://list` and the upstream docs. |

**Returns** — the upstream JSON envelope, with `data[].b64_json` rewritten as
a `data:image/<fmt>;base64,...` URL. The format is detected from the decoded
magic bytes, so it is accurate for jpeg, png, webp, gif, and bmp payloads.

**Example agent call**

```text
generate_image({
  "model": "flux-2-dev",
  "prompt": "A cute baby sea otter floating on its back, golden hour",
  "aspect_ratio": "16:9"
})
```

---

## Resources

### `image-models://list`

Returns a JSON list of every image-generation model ID currently available on
api.airforce, filtered to entries whose `media_type == "image"`. Cached for
5 minutes. Read this before calling `generate_image` to discover which
`model` values are valid right now.

```json
{
  "models": ["flux-2-dev", "nano-banana-pro", "..."],
  "count": 42
}
```

---

## Prompts

### `summarize`

A generic "summarize this text" prompt. Useful as a sanity check that the
prompt channel is wired up; not specific to image generation.

---

## Local development

```bash
# Clone and set up
git clone https://github.com/PurinNyova/Image-Generation-MCP.git
cd Image-Generation-MCP
uv sync
cp .env.example .env       # then fill in AIRFORCE_API_KEY

# Run the server
uv run image-generation-mcp

# Or run the bare module (no console script)
uv run python server.py
```

The server logs to **stderr** only — stdout is reserved for the MCP JSON-RPC
stream, so any stray `print` will break the protocol.

### Manual upstream tests

`test_scripts/` is excluded from git but contains two diagnostic harnesses
that exercise the api.airforce endpoints directly, independent of MCP:

```bash
# List image models
uv run python test_scripts/test.py

# Generate one image and write it to disk (auto-detects format)
uv run python test_scripts/image-generation-test.py
```

Override the test inputs via env vars: `TEST_MODEL`, `TEST_PROMPT`,
`TEST_ASPECT_RATIO`, `TEST_OUTPUT_PATH`.

---

## License

MIT. See [LICENSE](LICENSE).

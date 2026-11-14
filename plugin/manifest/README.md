# Plugin packaging notes

There is no separate manifest file to upload for a custom MCP connection in ChatGPT: per the OpenAI
developers quickstart (verified 2026-11-14) you add the server's public **HTTPS** `/mcp` URL under
ChatGPT → Plugins → "Add custom MCP server", configure authentication, accept the warning and create it
as a plugin. Tool names/descriptions/annotations come from the MCP server itself (`srm serve-mcp`).

The companion skill is in `plugin/skill/` (`SKILL.md`, `agents/openai.yaml`, `references/`). Zip that
folder if your ChatGPT plan offers skill upload. Publishing to a public directory is NOT required.
See `docs/OPENAI_INTEGRATION.md` for connection options, threat model and limitations.

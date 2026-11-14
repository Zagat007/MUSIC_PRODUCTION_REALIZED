# ChatGPT integration (optional)

Status: **AVAILABLE WHEN PLAN / PERMISSIONS / SECURE CONNECTIVITY ALLOW.** The desktop app and CLI are
fully functional without it. Nothing here requires payment from this project; whether your ChatGPT plan
allows custom MCP servers is outside this tool's control.

## What was verified (2026-11-14)
* OpenAI developers quickstart (VERIFIED): ChatGPT connects to a **public HTTPS** MCP endpoint
  (Streamable HTTP, path `/mcp`); add it under ChatGPT → Plugins → "Add custom MCP server", configure
  authentication, accept the warning, "Create as a plugin"; refresh the connection after tool changes.
  Local dev is shown with a tunnel (e.g. ngrok). Tool annotations (`readOnlyHint`, `destructiveHint`,
  `openWorldHint`, `idempotentHint`) are read by ChatGPT.
* Help-center articles on custom MCP rules and Skills: **NOT VERIFIED** (HTTP 403). Plan/admin/mobile
  availability must be checked by the user.

## Running the server
```
srm serve-mcp --project "D:\Music\Projects\collection"            # binds 127.0.0.1:8787
```
On first start a token and a signing key are created in `<project>\mcp_secret.json` (delete the file to
rotate). Authentication: `Authorization: Bearer <token>`, or — for clients that cannot send headers — the
capability URL `https://<your-host>/s/<token>/mcp` (weaker: the token is part of the URL and may appear in
proxy logs). Set `SRM_PUBLIC_BASE_URL=https://<your-host>` so download links use the public address, and
`SRM_ALLOWED_HOSTS=<your-host>` for DNS-rebinding protection.

## Option A — secure tunnel to your PC
Cloudflare Tunnel (free tier, needs a Cloudflare account) or ngrok (free tier, needs an account) forwards
HTTPS to `127.0.0.1:8787`. Threat model: anyone with the URL+token can call the tools while the tunnel runs;
tools expose no paths, no shell, no deletion, no uploads of source audio; exports/approvals need the user's
phrase; downloads are 10-minute signed single-file links; per-client rate limit 120 req/min; all calls are
audit-logged. Stop the tunnel when not in use. Do not use a tunnel without the token.

## Option B — separately hosted gateway
A small reverse proxy you host (e.g. on an existing home server) terminates TLS and forwards to the PC over
a private network (WireGuard/Tailscale). Same server, same token. Threat model adds the gateway host.

## Not done (by policy)
No public exposure is created automatically; no "insecure tunnel to claim success". The end-to-end test runs
a real MCP client against a real server on localhost (`tests/integration/test_m6_mcp.py`); a live ChatGPT
connection was **NOT VERIFIED** in this environment (requires the user's ChatGPT account and a public URL).

## Fallback
If MCP is unavailable (plan, admin policy, mobile, no tunnel): use the GUI/CLI, and paste the HTML/JSON
reports into ChatGPT for discussion. The skill in `plugin/skill/` still documents the safe workflow.

## Tools
`health_and_capabilities, list_projects, list_albums, list_tracks, scan_library(root_id),
analyze_track, analyze_album, get_analysis_report, suggest_mastering, create_preview,
approve_processing_version(confirmation), start_batch_render, get_job_status, cancel_job,
validate_release, export_album(confirmation), get_release_manifest, get_artifact_download`.

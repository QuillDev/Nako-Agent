# Nako CLI Reference

Live sources when anything looks stale: `nako --help`, `nako <command> --help`,
https://portal.nakode.moe/docs/reference/cli-commands

### Global Flags

```
nako [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
nako chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
nako setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
nako model                Interactive model/provider picker
nako fallback [add|remove|list]  Fallback provider chain
nako config [show|edit|get|set|unset|path|env-path|check|migrate]
nako login / logout       OAuth sign-in / clear stored auth
nako doctor [--fix]       Check dependencies and config
nako status [--full]      Component summary (--full: every section)
```

### Tools & Skills

```
nako tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

nako skills list|browse|search QUERY|inspect ID
nako skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
nako skills config        Enable/disable skills per platform
nako skills check|update|uninstall|publish PATH
nako skills tap add REPO  Add a GitHub repo as a skill source
nako bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
nako mcp add NAME (--url or --command) | remove | list | test NAME
nako mcp catalog | install NAME     Curated catalog install
nako mcp configure NAME             Toggle tool selection
nako mcp-serve                      Run Nako as an MCP server (mcp-serve plugin)
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
nako gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `nako photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://portal.nakode.moe/docs/user-guide/messaging/

### Sessions

```
nako sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
nako cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
nako webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
nako profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
nako profile rename A B | alias NAME | export NAME | import FILE
nako profile migrate-identity A B   Retry a completed rename's session/routing identity migration
```

### Credentials & Pools

```
nako auth                 Interactive credential manager
nako auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
nako auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
nako desktop / gui        Native desktop app
nako dashboard            Web admin panel + embedded chat (--stop / --status)
nako proxy                OpenAI-compatible local proxy backed by an OAuth provider
nako portal               Quick setup / sign in via Nous Portal
nako kanban <verb>        Multi-agent work-queue board
nako project              Named multi-folder workspaces (see references/projects-and-folders.md)
nako skin list|use|set    Switch/tweak skins (see references/themes.md)
nako pets <verb>          Pet mascots (see references/petdex.md)
nako memory setup|status|off|reset   Memory provider
nako secrets bitwarden|onepassword   External secret stores
nako moa                  Mixture-of-Agents slots
nako hooks / security / backup / import / checkpoints / console
nako logs [-f] [errors]   View agent/error logs
nako send                 One-off message through a gateway platform
nako pairing / plugins / insights / journey / computer-use
nako acp                  ACP server (IDE integration)
nako completion bash|zsh|fish
nako update / uninstall
```

Plugin- and provider-supplied subcommands (e.g. `nako photon setup`) only appear once their plugin is installed/active. Importing from OpenClaw, Claude Code, Codex CLI or Cursor (`nako import-agent <agent>`, plus the older `nako claw migrate|cleanup`) comes from the `agent-importers` plugin; a fresh `nako setup` that finds one of those agents offers to install it.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `nako config edit` · [Configuration docs](https://portal.nakode.moe/docs/user-guide/configuration) |
| Tools / toolsets | `nako tools list` · [Tools reference](https://portal.nakode.moe/docs/reference/tools-reference) |
| Skills catalog | `nako skills browse` · [Skills catalog](https://portal.nakode.moe/docs/reference/skills-catalog) |
| Provider setup | `nako model` · [Providers guide](https://portal.nakode.moe/docs/integrations/providers) |
| Env variables | `nako config env-path` · [Env vars reference](https://portal.nakode.moe/docs/reference/environment-variables) |
| Gateway logs | `~/.nako/logs/gateway.log` (or `nako logs`) |
| Sessions | `nako sessions browse` (reads state.db) |

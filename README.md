# unii-router

Run [UniiChat](https://uniichat.com) with its model traffic routed to
any Anthropic-compatible provider — with your own API key — instead of
Anthropic. Ships with [DeepSeek](https://api-docs.deepseek.com/), Kimi, and
Z.ai presets; bring any other Anthropic-flavored endpoint (OpenRouter, a local
shim, or another vendor) through the custom preset.

Unii's server hardcodes Anthropic's endpoint, so this package runs a local MITM
proxy that intercepts `api.anthropic.com` and rewrites requests to your
provider's Anthropic-compatible endpoint (e.g. DeepSeek's
`/anthropic/v1/messages`).

## What it does

- **Intercepts** `api.anthropic.com`, maps Claude model IDs to your
  provider's models, rewrites tool versions your provider doesn't accept, and
  swaps auth headers to match. All of it is per-provider: built-in presets
  configure DeepSeek, Kimi, and Z.ai; custom providers configure their own via
  `models`, `auth`, and `web_search_tool`.
- **Blocks** `api.openai.com` (configurable via `BLOCK_HOSTS`) with 403.
- **Blind-tunnels** every other host unchanged — no decryption, no
  interference. Non-model networking keeps working.
- Unii sanitizes the environment it gives shell-tool children (verified: they
  see no `HTTPS_PROXY` and connect directly), so the proxy only ever sees
  Unii's own server-side calls.
- The optional `urc` wrapper force-sets `UNII_URL` to the
  local server, strips proxy variables, and refuses non-local URL arguments —
  strace-verified to connect only to `127.0.0.1`.

Sign-in is Unii's own client flow and is unchanged. The router never handles
sign-in, and in server mode the client stores its token under
`~/.unii/<host:port>/` and does not prompt again.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/hammerhoundai/unii-router/master/install.sh | sh
```

This clones the repo to `~/.local/share/unii-chat-router`, checks its
dependencies, and symlinks both commands into `~/.local/bin`.

To install from an existing checkout:

```sh
git clone https://github.com/hammerhoundai/unii-router.git
cd unii-router
./install.sh
```

## Usage

```sh
unii-router            # proxy on 127.0.0.1:8899 + `unii serve` on 8788
unii-router 9000       # server on port 9000

If either local port is already occupied, the server exits before starting
anything. If that is an existing unii-router instance, connect with `urc`.

# in another terminal:
urc                     # leak-proof, forces http://127.0.0.1:8788
urc 9000
# or plain unii with UNII_URL set:
UNII_URL=http://127.0.0.1:8788 unii
```

The first server start generates a local CA under
`~/.local/state/unii-chat-router/pki` (mode `0700`); the CA is trusted only by
the launched process tree via `NODE_EXTRA_CA_CERTS` — nothing is installed into
the system trust store. The last model request is written `0600` to
`~/.local/state/unii-chat-router/last-request.json` for debugging.

### Model mapping

Unii requests Claude model IDs. The router rewrites them using the active
provider's `providers.<name>.models` map:

| Unii asks for          | Router uses                          |
|------------------------|--------------------------------------|
| `claude-opus-5-5`      | the matching provider model          |
| `claude-sonnet-5-5`    | the matching provider model          |
| `claude-haiku-5-5`     | the matching provider model          |
| any other model ID     | the same ID, unless `"*"` is mapped  |

Every built-in preset supplies its own defaults; see
[Providers and presets](#providers-and-presets).

## Environment variables

| Variable                     | Default            | Purpose |
|------------------------------|--------------------|---------|
| provider key variable        | `DEEPSEEK_API_KEY`, `KIMI_API_KEY`, or `ZAI_API_KEY` | Read when the selected preset's `env_key` names it; custom providers can name any variable |
| `UNII_PORT` / arg            | `8788`             | Local Unii server port |
| `UNII_CHAT_ROUTER_PROXY_PORT`| `8899`             | Local MITM proxy port |
| `HIJACK_HOSTS`               | `api.anthropic.com`| Intercepted hosts when config doesn't set `hijack_hosts` |
| `BLOCK_HOSTS`                | `api.openai.com`   | Hosts rejected with 403 |
| `LAST_REQUEST`               | state dir file     | Where the last request body is dumped (`0600`); empty disables |
| `UNII_NO_TELEMETRY`          | (not set)          | Opt out of Unii's daily update check; normally set via `unii_no_telemetry` in the config file |

## Configuration file

Settings live in `~/.config/unii-chat-router/config.json` (override the path
with `UNII_CHAT_ROUTER_CONFIG`). The file is auto-created with defaults on
first start; anything not configured keeps its built-in default, and unknown
keys or bad values are ignored with a warning in the proxy log.

```json
{
  "upstream_connect_timeout": 30,
  "tunnel_connect_timeout": 15
}
```

| Key                       | Default | Purpose |
|---------------------------|---------|---------|
| `upstream_connect_timeout`| `30`    | Seconds to connect + TLS-handshake to the model provider |
| `tunnel_connect_timeout`  | `15`    | Seconds to connect for blind-tunneled non-model hosts |
| `unii_no_telemetry`       | `false` | `true` sets `UNII_NO_TELEMETRY=1` for the Unii server (opts out of Unii's daily update check to bend-lang.com) |

Timeouts apply only to connection setup; streaming responses are never killed
by an idle timeout. Telemetry is Unii's own (version + OS + CPU type only);
it is on by default and the router does not touch it unless you opt out.

### Providers and presets

`active_provider` selects the upstream model provider, and each provider has
its own section under `providers`. Built-in preset names:

- `"deepseek"` (default) — `https://api.deepseek.com/anthropic`, Bearer auth,
  key from `DEEPSEEK_API_KEY`
- `"kimi"` — Moonshot Kimi coding plan: `https://api.kimi.ai/coding`,
  `x-api-key` auth, key from `KIMI_API_KEY`, all roles → `k3-256k`
- `"zai"` — Z.ai coding plan: `https://api.z.ai/api/anthropic`, Bearer auth,
  key from `ZAI_API_KEY`, all roles → `glm-5.3-flash`
- `"custom"` — your own Anthropic-compatible provider (any vendor or a local
  shim speaking the Anthropic messages API)

The auto-generated default file looks like this (`active_provider` selects
DeepSeek; the other preset sections are present so they can be edited or
selected without being copied from documentation; `providers.custom` holds
placeholders):

```json
{
  "active_provider": "deepseek",
  "upstream_connect_timeout": 30,
  "tunnel_connect_timeout": 15,
  "unii_no_telemetry": false,
  "providers": {
    "deepseek": {
      "base_url": "https://api.deepseek.com/anthropic",
      "env_key": "DEEPSEEK_API_KEY",
      "auth": "bearer",
      "models": {
        "claude-opus-5-5": "deepseek-flash",
        "claude-sonnet-5-5": "deepseek-flash",
        "claude-haiku-5-5": "deepseek-flash"
      },
      "hijack_hosts": ["api.anthropic.com"],
      "web_search_tool": "20260209"
    },
    "kimi": {
      "base_url": "https://api.kimi.ai/coding",
      "env_key": "KIMI_API_KEY",
      "auth": "x-api-key",
      "models": {
        "claude-opus-5-5": "k3-256k",
        "claude-sonnet-5-5": "k3-256k",
        "claude-haiku-5-5": "k3-256k",
        "*": "k3-256k"
      },
      "hijack_hosts": ["api.anthropic.com"],
      "web_search_tool": "20260209"
    },
    "zai": {
      "base_url": "https://api.z.ai/api/anthropic",
      "env_key": "ZAI_API_KEY",
      "auth": "bearer",
      "models": {
        "claude-opus-5-5": "glm-5.3-flash",
        "claude-sonnet-5-5": "glm-5.3-flash",
        "claude-haiku-5-5": "glm-5.3-flash",
        "*": "glm-5.3-flash"
      },
      "hijack_hosts": ["api.anthropic.com"],
      "web_search_tool": "20260209"
    },
    "custom": {
      "base_url": "FILL_THIS_IF_USING_CUSTOM_PRESET",
      "env_key": "FILL_THIS_IF_USING_CUSTOM_PRESET",
      "auth": "bearer",
      "models": {},
      "hijack_hosts": ["api.anthropic.com"],
      "web_search_tool": "20260209"
    }
  }
}
```

To use a provider without a built-in preset, set
`"active_provider": "custom"` and fill the custom section:

```json
{
  "active_provider": "custom",
  "providers": {
    "custom": {
      "base_url": "https://anthropic.example.com",
      "env_key": "EXAMPLE_PROVIDER_API_KEY",
      "auth": "bearer",
      "models": {
        "claude-opus-5-5": "example-large",
        "claude-sonnet-5-5": "example-large",
        "claude-haiku-5-5": "example-small",
        "*": "example-large"
      },
      "hijack_hosts": ["api.anthropic.com"],
      "web_search_tool": "20260209"
    }
  }
}
```

| Field | Default | Purpose |
|-------|---------|---------|
| `base_url` | (required for custom) | Upstream endpoint; `http://` allowed for local shims |
| `api_key` | — | Key taken directly from the config file |
| `env_key` | — | Preferred: name of the env var holding the key |
| `auth` | `bearer` | `bearer` or `x-api-key` |
| `models` | `{}` | `claude-id → provider-model`; `"*"` is the fallback; unmapped IDs pass through |
| `hijack_hosts` | `api.anthropic.com` | CONNECT targets intercepted and rewritten |
| `web_search_tool` | `20260209` | Rewrites Unii's `web_search_*` tool to this version; `null` disables rewriting |

Preset reference (all presets intercept `api.anthropic.com`; every field can
be overridden in the provider's `providers.<name>` section):

| Preset | Base URL | Auth | Key env var | Models |
|--------|----------|------|-------------|--------|
| `deepseek` | `https://api.deepseek.com/anthropic` | bearer | `DEEPSEEK_API_KEY` | opus/sonnet → `deepseek-flash`, haiku → `deepseek-flash` |
| `kimi` | `https://api.kimi.ai/coding` | `x-api-key` | `KIMI_API_KEY` | all roles → `k3-256k` |
| `zai` | `https://api.z.ai/api/anthropic` | bearer | `ZAI_API_KEY` | all roles → `glm-5.3-flash` |

Preset sections can override any built-in default, e.g.
`"providers": {"deepseek": {"models": {"*": "deepseek-v4-pro"}}}`. While a
section still contains `FILL_THIS_IF_USING_CUSTOM_PRESET` placeholders, the
router warns and falls back to the deepseek preset. Prefer `env_key` over
`api_key` so secrets stay out of the config file; keys are never printed in
logs. The deprecated flat layout (`"provider": …` with top-level sections)
still works but logs a rename warning.

## Requirements

- Linux (optionally macOS; the MITM proxy is plain Python)
- `python3` with the `cryptography` package
- `unii` in `PATH` (https://uniichat.com/install.sh)
- an API key for your chosen provider: `DEEPSEEK_API_KEY`, `KIMI_API_KEY`, or
  `ZAI_API_KEY` for the built-in presets, or any Anthropic-compatible endpoint
  + key via the custom preset (see
  [Providers and presets](#providers-and-presets))


## Optional network guard

`landlock-exec.py` applies Linux Landlock's deny-by-default TCP-connect policy
to a process tree (inherited by all children; UDP/DNS not covered):

```sh
LANDLOCK_ALLOW_CONNECT=8788,8899 ./landlock-exec.py -- unii-router
```

It is intentionally not wired into the default launcher, because it would also
restrict network access of Unii's shell tools.

## Limitations

- Client cancellation is not propagated to the upstream provider request.
- Unii versions may change endpoints/tools at any time; tested against
  Unii 1.0.136/1.0.137.
- Shell tools inside Unii agents can still reach the internet directly (that
  is a property of Unii, not this router); use the Landlock guard or a network
  namespace if you need to constrain them.
- Single process, no daemon/systemd integration.

## License

MIT

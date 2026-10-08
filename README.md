# unii-chat-router

Run [UniiChat](https://uniichat.com) with its model traffic routed to
[DeepSeek](https://api-docs.deepseek.com/) instead of Anthropic, using your own
`DEEPSEEK_API_KEY`.

Unii's server hardcodes Anthropic's endpoint, so this package runs a local MITM
proxy that intercepts `api.anthropic.com` and rewrites requests to DeepSeek's
Anthropic-compatible API (`/anthropic/v1/messages`).

## What it does

- **Intercepts** `api.anthropic.com` (configurable via `HIJACK_HOSTS`), maps
  Claude model IDs to DeepSeek models, rewrites Unii's
  `web_search_20260318` tool to DeepSeek's accepted `web_search_20260209`,
  and swaps `x-api-key` auth for DeepSeek Bearer auth.
- **Blocks** `api.openai.com` (configurable via `BLOCK_HOSTS`) with 403.
- **Blind-tunnels** every other host unchanged — no decryption, no
  interference. Non-model networking keeps working.
- Unii sanitizes the environment it gives shell-tool children (verified: they
  see no `HTTPS_PROXY` and connect directly), so the proxy only ever sees
  Unii's own server-side calls.
- The optional `unii-chat-router-client` wrapper force-sets `UNII_URL` to the
  local server, strips proxy variables, and refuses non-local URL arguments —
  strace-verified to connect only to `127.0.0.1`.

Sign-in is Unii's own client flow and is unchanged. The router never handles
sign-in, and in server mode the client stores its token under
`~/.unii/<host:port>/` and does not prompt again.

## Requirements

- Linux (optionally macOS; the MITM proxy is plain Python)
- `python3` with the `cryptography` package
- `unii` in `PATH` (https://uniichat.com/install.sh)
- `DEEPSEEK_API_KEY` with access to a DeepSeek Anthropic-compatible model
  (`deepseek-flash`, `deepseek-flash[1m]`)

## Install

```sh
git clone https://github.com/YOURNAME/unii-chat-router.git
cd unii-chat-router
./install.sh
```

`install.sh` checks dependencies and symlinks `unii-chat-router`,
`unii-chat-router-client` (plus legacy `unii-deepseek` aliases) into
`~/.local/bin`.

## Usage

```sh
unii-chat-router            # proxy on 127.0.0.1:8899 + `unii serve` on 8788
unii-chat-router 9000       # server on port 9000

# in another terminal — either:
unii-chat-router-client     # leak-proof, forces http://127.0.0.1:8788
unii-chat-router-client 9000
# or plain unii with UNII_URL set:
UNII_URL=http://127.0.0.1:8788 unii
```

The first server start generates a local CA under
`~/.local/state/unii-chat-router/pki` (mode `0700`); the CA is trusted only by
the launched process tree via `NODE_EXTRA_CA_CERTS` — nothing is installed into
the system trust store. The last model request is written `0600` to
`~/.local/state/unii-chat-router/last-request.json` for debugging.

### Model mapping

| Unii asks for        | Router sends          |
|----------------------|-----------------------|
| `claude-opus-5-5`    | `deepseek-flash[1m]`  |
| `claude-sonnet-5-5`  | `deepseek-flash[1m]`  |
| `claude-haiku-5-5`   | `deepseek-flash`      |

Unmapped models pass through unchanged.

## Environment variables

| Variable                     | Default            | Purpose |
|------------------------------|--------------------|---------|
| `DEEPSEEK_API_KEY`           | (required)         | DeepSeek key; also used as `ANTHROPIC_API_KEY` for Unii |
| `UNII_PORT` / arg            | `8788`             | Local Unii server port |
| `UNII_CHAT_ROUTER_PROXY_PORT`| `8899`             | Local MITM proxy port |
| `HIJACK_HOSTS`               | `api.anthropic.com`| Hosts intercepted and rewritten to DeepSeek |
| `BLOCK_HOSTS`                | `api.openai.com`   | Hosts rejected with 403 |
| `LAST_REQUEST`               | state dir file     | Where the last request body is dumped (`0600`); empty disables |
| `UNII_NO_TELEMETRY`          | `1`                | Disables Unii's daily update check (bend-lang.com) |

## Optional network guard

`landlock-exec.py` applies Linux Landlock's deny-by-default TCP-connect policy
to a process tree (inherited by all children; UDP/DNS not covered):

```sh
LANDLOCK_ALLOW_CONNECT=8788,8899 ./landlock-exec.py -- unii-chat-router
```

It is intentionally not wired into the default launcher, because it would also
restrict network access of Unii's shell tools.

## Limitations

- Client cancellation is not propagated to the upstream DeepSeek request.
- Unii versions may change endpoints/tools at any time; tested against
  Unii 1.0.136/1.0.137.
- Shell tools inside Unii agents can still reach the internet directly (that
  is a property of Unii, not this router); use the Landlock guard or a network
  namespace if you need to constrain them.
- Single process, no daemon/systemd integration.

## License

MIT

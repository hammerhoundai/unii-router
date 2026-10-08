#!/usr/bin/env python3
import copy, datetime, errno, json, os, socket, ssl, sys, threading, traceback
from urllib.parse import urlsplit
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

LISTEN = ("127.0.0.1", int(os.environ.get("PROXY_PORT", "8899")))
CA_KEY_PATH = os.environ.get("CA_KEY", "/tmp/unii-ds-ca.key")
CA_CERT_PATH = os.environ.get("CA_CERT", "/tmp/unii-ds-ca.pem")
LEAF_CERT_PATH = os.environ.get("LEAF_CERT", "/tmp/unii-ds-leaf.pem")
LEAF_KEY_PATH = os.environ.get("LEAF_KEY", "/tmp/unii-ds-leaf.key")
BLOCK_HOSTS = {x for x in os.environ.get("BLOCK_HOSTS", "api.openai.com").split(",") if x}
LAST_REQUEST = os.environ.get("LAST_REQUEST", "")
LOG = threading.Lock()

CONFIG_PATH = os.environ.get(
    "UNII_CHAT_ROUTER_CONFIG",
    os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                 "unii-chat-router", "config.json"),
)
DEFAULT_UPSTREAM_CONNECT_TIMEOUT = 30.0
DEFAULT_TUNNEL_CONNECT_TIMEOUT = 15.0
DEFAULT_WEB_SEARCH_TOOL = "20260209"
DEFAULT_HIJACK_HOSTS = ["api.anthropic.com"]
DEFAULT_UNII_NO_TELEMETRY = False

# Built-in provider presets. `custom` is user-defined in the config file;
# kimi/zai are reserved for future built-in presets.
PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "env_key": "DEEPSEEK_API_KEY",
        "auth": "bearer",
        "models": {
            "claude-opus-5-5": "deepseek-flash",
            "claude-sonnet-5-5": "deepseek-flash",
            "claude-haiku-5-5": "deepseek-flash",
        },
        "hijack_hosts": DEFAULT_HIJACK_HOSTS,
        "web_search_tool": DEFAULT_WEB_SEARCH_TOOL,
    },
    "kimi": {
        "base_url": "https://api.kimi.ai/coding",
        "env_key": "KIMI_API_KEY",
        "auth": "x-api-key",
        "models": {
            "claude-opus-5-5": "k3-256k",
            "claude-sonnet-5-5": "k3-256k",
            "claude-haiku-5-5": "k3-256k",
            "*": "k3-256k",
        },
        "hijack_hosts": DEFAULT_HIJACK_HOSTS,
        "web_search_tool": DEFAULT_WEB_SEARCH_TOOL,
    },
    "zai": {
        "base_url": "https://api.z.ai/api/anthropic",
        "env_key": "ZAI_API_KEY",
        "auth": "bearer",
        "models": {
            "claude-opus-5-5": "glm-5.3-flash",
            "claude-sonnet-5-5": "glm-5.3-flash",
            "claude-haiku-5-5": "glm-5.3-flash",
            "*": "glm-5.3-flash",
        },
        "hijack_hosts": DEFAULT_HIJACK_HOSTS,
        "web_search_tool": DEFAULT_WEB_SEARCH_TOOL,
    },
}
PRESET_NAMES = set(PRESETS)
PLACEHOLDER = "FILL_THIS_IF_USING_CUSTOM_PRESET"

def log(*a):
    with LOG:
        print(*a, flush=True)

def is_placeholder(v):
    return isinstance(v, str) and v.strip().upper() == PLACEHOLDER

def default_config_template():
    providers = copy.deepcopy(PRESETS)
    providers["custom"] = {
        "base_url": PLACEHOLDER,
        "env_key": PLACEHOLDER,
        "auth": "bearer",
        "models": {},
        "hijack_hosts": DEFAULT_HIJACK_HOSTS,
        "web_search_tool": DEFAULT_WEB_SEARCH_TOOL,
    }
    return {
        "active_provider": "deepseek",
        "upstream_connect_timeout": int(DEFAULT_UPSTREAM_CONNECT_TIMEOUT),
        "tunnel_connect_timeout": int(DEFAULT_TUNNEL_CONNECT_TIMEOUT),
        "unii_no_telemetry": DEFAULT_UNII_NO_TELEMETRY,
        "providers": providers,
    }

def write_default_config_if_missing():
    if os.path.exists(CONFIG_PATH):
        return
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "x", encoding="utf-8") as f:
            json.dump(default_config_template(), f, indent=2)
            f.write("\n")
        os.chmod(CONFIG_PATH, 0o600)
        log(f"CONFIG: created default config at {CONFIG_PATH} "
            f"(fill providers.custom to use a different provider)")
    except OSError as e:
        log(f"CONFIG warning: could not create default config at {CONFIG_PATH}: {e!r}")

def load_config():
    write_default_config_if_missing()
    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"CONFIG warning: ignoring unreadable {CONFIG_PATH}: {e!r}")
        return {}
    if not isinstance(cfg, dict):
        log(f"CONFIG warning: {CONFIG_PATH} is not a JSON object; ignoring")
        return {}
    return cfg

def positive_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0

def timeout_value(cfg, key, default):
    v = cfg.get(key, default)
    if positive_number(v):
        return float(v)
    if key in cfg:
        log(f"CONFIG warning: ignoring {key}={v!r} (must be a positive number of seconds)")
    return default

def bool_value(cfg, key, default):
    if key not in cfg:
        return default
    v = cfg[key]
    if isinstance(v, bool):
        return v
    log(f"CONFIG warning: ignoring {key}={v!r} (must be true or false)")
    return default

def scrub_secrets(cfg):
    c = json.loads(json.dumps(cfg))
    sections = [c] + [c[k] for k in list(c) if isinstance(c.get(k), dict)]
    for s in sections:
        if isinstance(s, dict) and "api_key" in s:
            s["api_key"] = "***"
    return c

def build_provider(name, base, origin):
    u = urlsplit(base.get("base_url", ""))
    scheme = (u.scheme or "").lower()
    if scheme not in ("http", "https") or not u.hostname:
        log(f"CONFIG warning: {origin}.base_url is missing or invalid; falling back to the deepseek preset")
        return None
    key = base.get("api_key")
    if not (isinstance(key, str) and key.strip()):
        env_name = base.get("env_key")
        key = os.environ.get(env_name, "") if isinstance(env_name, str) and env_name else ""
    if not (isinstance(key, str) and key.strip()):
        log(f"CONFIG warning: no API key for provider {name!r} (set api_key or env_key); "
            f"falling back to the deepseek preset")
        return None
    auth = base.get("auth", "bearer")
    if auth not in ("bearer", "x-api-key"):
        log(f"CONFIG warning: {origin}.auth={auth!r} invalid (bearer|x-api-key); using bearer")
        auth = "bearer"
    models = base.get("models") if isinstance(base.get("models"), dict) else {}
    models = {str(k): str(v) for k, v in models.items() if isinstance(v, str) and v}
    hijack = base.get("hijack_hosts")
    if not (isinstance(hijack, list) and hijack and all(isinstance(h, str) and h for h in hijack)):
        hijack = None  # explicit only; env/default applies otherwise
    ws = base.get("web_search_tool", DEFAULT_WEB_SEARCH_TOOL)
    if ws is not None and not (isinstance(ws, str) and ws):
        log(f"CONFIG warning: {origin}.web_search_tool={ws!r} invalid (string or null); using default")
        ws = DEFAULT_WEB_SEARCH_TOOL
    return {
        "name": name,
        "scheme": scheme,
        "host": u.hostname,
        "port": u.port or (443 if scheme == "https" else 80),
        "prefix": (u.path or "").rstrip("/"),
        "key": key.strip(),
        "auth": auth,
        "models": models,
        "hijack_hosts": hijack,
        "web_search_tool": ws,
    }

def resolve_provider(cfg):
    whitelist = {"active_provider", "provider", "providers", "custom", "deepseek",
                 "upstream_connect_timeout", "tunnel_connect_timeout",
                 "unii_no_telemetry"} | PRESET_NAMES
    unknown = sorted(k for k in cfg if k not in whitelist)
    if unknown:
        log("CONFIG warning: ignoring unknown keys: " + ", ".join(unknown))
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    if "provider" in cfg:
        log("CONFIG warning: 'provider' is deprecated; rename it to 'active_provider'")
    name = cfg.get("active_provider", cfg.get("provider", "deepseek"))
    if not isinstance(name, str) or not name:
        log(f"CONFIG warning: ignoring provider={name!r}")
        name = "deepseek"
    if is_placeholder(name):
        log("CONFIG warning: active_provider is a placeholder; using the deepseek preset")
        name = "deepseek"

    def section(pname):
        """User section for a provider: providers.<name>, or deprecated top-level <name>."""
        if pname in providers and isinstance(providers[pname], dict):
            return providers[pname], None
        if pname in cfg and isinstance(cfg[pname], dict):
            return cfg[pname], "deprecated: move it under 'providers'"
        return None, None

    def configured(section_dict):
        """False while a section still carries template placeholder values."""
        return not any(is_placeholder(v) for v in section_dict.values())

    if name == "custom":
        c, dep = section("custom")
        if isinstance(c, dict) and c:
            if dep:
                log(f"CONFIG warning: top-level '{'custom'}' section is {dep}")
            if not configured(c):
                log("CONFIG warning: custom provider still contains placeholder values; "
                    "fill in providers.custom to activate it")
            else:
                p = build_provider("custom", c, "custom")
                if p:
                    return p
        else:
            log("CONFIG warning: provider=custom but the custom object is missing or empty")
        log("CONFIG warning: using the built-in deepseek preset instead")
        name = "deepseek"
    elif name in PRESET_NAMES and name not in PRESETS:
        log(f"CONFIG warning: provider {name!r} is reserved but not available in this version; "
            f"using the built-in deepseek preset")
        name = "deepseek"
    elif name not in PRESET_NAMES:
        log(f"CONFIG warning: unknown provider {name!r}; using the built-in deepseek preset")
        name = "deepseek"
    if name in PRESETS:
        overrides, dep = section(name)
        if dep:
            log(f"CONFIG warning: top-level '{name}' section is {dep}")
        overrides = overrides or {}
        base = {**PRESETS[name], **overrides}
        p = build_provider(name, base, name)
        if p:
            return p
    p = build_provider("deepseek", PRESETS["deepseek"], "deepseek")
    if p:
        return p
    log("CONFIG error: no API key available for any provider "
        "(set DEEPSEEK_API_KEY, or api_key/env_key in the config file)")
    sys.exit(2)

RAW_CONFIG = load_config()
UPSTREAM_CONNECT_TIMEOUT = timeout_value(RAW_CONFIG, "upstream_connect_timeout", DEFAULT_UPSTREAM_CONNECT_TIMEOUT)
TUNNEL_CONNECT_TIMEOUT = timeout_value(RAW_CONFIG, "tunnel_connect_timeout", DEFAULT_TUNNEL_CONNECT_TIMEOUT)
UNII_NO_TELEMETRY = bool_value(RAW_CONFIG, "unii_no_telemetry", DEFAULT_UNII_NO_TELEMETRY)
PROVIDER = resolve_provider(RAW_CONFIG)
HIJACK_HOSTS = set(
    PROVIDER["hijack_hosts"]
    or [x.strip() for x in os.environ.get("HIJACK_HOSTS", "").split(",") if x.strip()]
    or DEFAULT_HIJACK_HOSTS
)

if "--resolve-key" in sys.argv:
    print(PROVIDER["name"])
    print(PROVIDER["key"])
    print(1 if UNII_NO_TELEMETRY else 0)
    sys.exit(0)

def name(common):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

def make_ca():
    if os.path.exists(CA_CERT_PATH) and os.path.exists(CA_KEY_PATH):
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    cert = (x509.CertificateBuilder()
        .subject_name(name("Unii Chat Router Local CA"))
        .issuer_name(name("Unii Chat Router Local CA"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=True, crl_sign=True,
                                     encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256()))
    open(CA_KEY_PATH, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    open(CA_CERT_PATH, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(CA_KEY_PATH, 0o600)

def make_leaf(hosts):
    hosts = sorted(set(hosts))
    if not hosts:
        raise RuntimeError("no hijack hosts configured; refusing to issue an empty leaf certificate")
    if os.path.exists(LEAF_CERT_PATH) and os.path.exists(LEAF_KEY_PATH):
        try:
            cert = x509.load_pem_x509_certificate(open(LEAF_CERT_PATH, "rb").read())
            have = {x.value for x in cert.extensions
                    .get_extension_for_class(x509.SubjectAlternativeName).value}
            if have == set(hosts):
                return
            log("leaf certificate host set changed; regenerating")
        except Exception:
            log("existing leaf certificate unreadable; regenerating")
    ca_key = serialization.load_pem_private_key(open(CA_KEY_PATH, "rb").read(), None)
    ca_cert = x509.load_pem_x509_certificate(open(CA_CERT_PATH, "rb").read())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    cert = (x509.CertificateBuilder()
        .subject_name(name(hosts[0]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                     key_encipherment=True, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .sign(ca_key, hashes.SHA256()))
    open(LEAF_KEY_PATH, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    open(LEAF_CERT_PATH, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(LEAF_KEY_PATH, 0o600)

make_ca()
make_leaf(HIJACK_HOSTS)
server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
server_tls.set_alpn_protocols(["http/1.1"])
server_tls.load_cert_chain(LEAF_CERT_PATH, LEAF_KEY_PATH)
upstream_tls = ssl.create_default_context()

def recv_until_headers(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        b = sock.recv(65536)
        if not b:
            break
        data += b
        if len(data) > 1024 * 1024:
            raise ValueError("request headers too large")
    return data

def parse_headers(raw):
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin1").split("\r\n")
    method, target, version = lines[0].split(" ", 2)
    headers = []
    for line in lines[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            headers.append((k.lower(), v))
    return method, target, version, headers, rest

def header(headers, k):
    k = k.lower()
    return next((v for x, v in headers if x == k), None)

def rewrite(body):
    obj = json.loads(body)
    if "model" in obj:
        m = obj["model"]
        obj["model"] = PROVIDER["models"].get(m, PROVIDER["models"].get("*", m))
    tools = obj.get("tools")
    ws = PROVIDER["web_search_tool"]
    if isinstance(tools, list) and isinstance(ws, str) and ws:
        for t in tools:
            if isinstance(t, dict) and isinstance(t.get("type"), str) and t["type"].startswith("web_search_"):
                t["type"] = f"web_search_{ws}"
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()

def relay(src, dst):
    while True:
        b = src.recv(65536)
        if not b:
            break
        dst.sendall(b)

def watch_model_client(client, up, finished):
    """Close the upstream request when the Unii client disappears.

    The request has already been forwarded in full, so any read on the client
    connection while the model response is active can only be a disconnect or
    an invalid early request. The polling is only for waking this watcher when
    a normal response completes.
    """
    try:
        client.settimeout(0.1)
        while not finished.is_set():
            try:
                data = client.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                log("MODEL-CANCEL client connection failed")
                return
            if not data:
                log("MODEL-CANCEL client closed connection")
                return
            log("MODEL-CANCEL unexpected client data")
            return
    finally:
        client.settimeout(None)
        if not finished.is_set():
            try:
                up.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

def copy(src, dst):
    try:
        while True:
            b = src.recv(65536)
            if not b:
                break
            dst.sendall(b)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

def blocked_response(conn, note):
    log("BLOCK", note)
    conn.sendall(b"HTTP/1.1 403 Blocked by Unii Chat Router proxy\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")

def forward_plain(conn, method, target, headers, body_rest):
    if target.startswith("/"):
        h = header(headers, "host") or ""
        host, _, port_s = h.partition(":")
        port = int(port_s) if port_s else 80
        path = target
    else:
        u = urlsplit(target)
        host, port = u.hostname, u.port or 80
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
    if not host:
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
        return
    if host in HIJACK_HOSTS or host in BLOCK_HOSTS:
        blocked_response(conn, f"{method} http://{host}{path}")
        return
    n = int(header(headers, "content-length") or 0)
    while len(body_rest) < n:
        b = conn.recv(65536)
        if not b:
            break
        body_rest += b
    body = body_rest[:n]
    out = [f"{method} {path} HTTP/1.1",
           f"Host: {host}" + (f":{port}" if port != 80 else "")]
    for k, v in headers:
        if k in ("host", "content-length", "connection", "proxy-connection", "keep-alive",
                 "transfer-encoding", "expect", "proxy-authorization"):
            continue
        out.append(f"{k}: {v}")
    out.append(f"content-length: {len(body)}")
    out.append("connection: close")
    req = ("\r\n".join(out) + "\r\n\r\n").encode("latin1") + body
    log(f"TUNNEL {method} http://{host}:{port}{path}")
    with socket.create_connection((host, port), timeout=UPSTREAM_CONNECT_TIMEOUT) as up:
        up.settimeout(None)
        up.sendall(req)
        resph = recv_until_headers(up)
        if not resph:
            return
        conn.sendall(resph)
        relay(up, conn)

def tunnel(conn, target):
    host, _, port_s = target.rpartition(":")
    if not port_s.isdigit():
        host, port = target, 443
    else:
        host = host.strip("[]")
        port = int(port_s)
    log(f"TUNNEL CONNECT {target}")
    with socket.create_connection((host, port), timeout=TUNNEL_CONNECT_TIMEOUT) as up:
        up.settimeout(None)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        t = threading.Thread(target=copy, args=(up, conn), daemon=True)
        t.start()
        copy(conn, up)
        t.join(timeout=5)

def send_and_relay(up, client, upstream_req):
    up.settimeout(None)
    finished = threading.Event()
    watcher = threading.Thread(target=watch_model_client,
                               args=(client, up, finished), daemon=True)
    watcher.start()
    try:
        up.sendall(upstream_req)
        resph = recv_until_headers(up)
        if not resph:
            return False
        log("MODEL-RS", resph.split(b"\r\n", 1)[0].decode("latin1"))
        client.sendall(resph)
        relay(up, client)
        return True
    finally:
        finished.set()
        watcher.join(timeout=1)
        try:
            client.settimeout(None)
        except OSError:
            pass

def proxy_anthropic(client):
    raw = recv_until_headers(client)
    if not raw:
        return False
    method, target, version, headers, body_rest = parse_headers(raw)
    if method != "POST":
        client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
        return False
    n = int(header(headers, "content-length") or 0)
    while len(body_rest) < n:
        b = client.recv(65536)
        if not b:
            break
        body_rest += b
    body = body_rest[:n]
    newbody = rewrite(body)
    if LAST_REQUEST:
        fd = os.open(LAST_REQUEST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(newbody)
    default_port = (PROVIDER["scheme"] == "https" and PROVIDER["port"] == 443) or \
                   (PROVIDER["scheme"] == "http" and PROVIDER["port"] == 80)
    host_header = PROVIDER["host"] + ("" if default_port else f":{PROVIDER['port']}")
    log(f"MODEL-RQ path={target} bytes={len(newbody)} -> {PROVIDER['scheme']}://{host_header}{PROVIDER['prefix']}{target}")
    if PROVIDER["auth"] == "x-api-key":
        auth_line = f"x-api-key: {PROVIDER['key']}"
    else:
        auth_line = f"Authorization: Bearer {PROVIDER['key']}"
    upstream_req = (
        f"POST {PROVIDER['prefix']}{target} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"{auth_line}\r\n"
        f"content-type: application/json\r\n"
        f"anthropic-version: 2023-06-01\r\n"
        f"content-length: {len(newbody)}\r\n"
        f"connection: close\r\n\r\n"
    ).encode("latin1") + newbody
    with socket.create_connection((PROVIDER["host"], PROVIDER["port"]),
                                  timeout=UPSTREAM_CONNECT_TIMEOUT) as plain:
        if PROVIDER["scheme"] == "https":
            with upstream_tls.wrap_socket(plain, server_hostname=PROVIDER["host"]) as up:
                return send_and_relay(up, client, upstream_req)
        return send_and_relay(plain, client, upstream_req)

def handle(conn, addr):
    try:
        req = recv_until_headers(conn)
        if not req:
            return
        method, target, version, headers, rest = parse_headers(req)
        if method != "CONNECT":
            forward_plain(conn, method, target, headers, rest)
            return
        host = target.rsplit(":", 1)[0].strip("[]")
        if host not in HIJACK_HOSTS:
            if host in BLOCK_HOSTS:
                blocked_response(conn, f"CONNECT {target} (blocked host)")
            else:
                tunnel(conn, target)
            return
        log(f"ALLOW CONNECT {target} (intercept; provider={PROVIDER['name']})")
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        with server_tls.wrap_socket(conn, server_side=True) as tls:
            while proxy_anthropic(tls):
                pass
    except (ssl.SSLError, ConnectionError, TimeoutError, OSError) as e:
        log("connection ended:", type(e).__name__, str(e))
    except Exception as e:
        log("ERROR", repr(e))
        traceback.print_exc()
    finally:
        try: conn.close()
        except Exception: pass

try:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(LISTEN)
except OSError as e:
    if e.errno == errno.EADDRINUSE:
        log(f"proxy port {LISTEN[0]}:{LISTEN[1]} is already in use; "
            "if unii-router is already running, use urc to connect to it")
        raise SystemExit(2)
    raise
s.listen(128)
default_port = (PROVIDER["scheme"] == "https" and PROVIDER["port"] == 443) or \
               (PROVIDER["scheme"] == "http" and PROVIDER["port"] == 80)
base_desc = (f"{PROVIDER['scheme']}://{PROVIDER['host']}"
             + ("" if default_port else f":{PROVIDER['port']}") + PROVIDER["prefix"])
log(f"listening on http://{LISTEN[0]}:{LISTEN[1]}; provider={PROVIDER['name']}; "
    f"upstream={base_desc}; intercept={sorted(HIJACK_HOSTS)}; auth={PROVIDER['auth']}; "
    f"upstream_connect_timeout={UPSTREAM_CONNECT_TIMEOUT:g}s; tunnel_connect_timeout={TUNNEL_CONNECT_TIMEOUT:g}s")
if RAW_CONFIG:
    log(f"CONFIG loaded from {CONFIG_PATH}: " + json.dumps(scrub_secrets(RAW_CONFIG), sort_keys=True))
while True:
    c, a = s.accept()
    threading.Thread(target=handle, args=(c,a), daemon=True).start()

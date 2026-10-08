#!/usr/bin/env python3
import datetime, json, os, socket, ssl, threading, traceback
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
UPSTREAM = "api.deepseek.com"
HIJACK_HOSTS = {x for x in os.environ.get("HIJACK_HOSTS", "api.anthropic.com").split(",") if x}
BLOCK_HOSTS = {x for x in os.environ.get("BLOCK_HOSTS", "api.openai.com").split(",") if x}
LAST_REQUEST = os.environ.get("LAST_REQUEST", "")
MODEL_MAP = {
    "claude-opus-5-5": "deepseek-flash[1m]",
    "claude-sonnet-5-5": "deepseek-flash[1m]",
    "claude-haiku-5-5": "deepseek-flash",
}
LOG = threading.Lock()

def log(*a):
    with LOG:
        print(*a, flush=True)

def name(common):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

def make_ca():
    if os.path.exists(CA_CERT_PATH) and os.path.exists(CA_KEY_PATH):
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    cert = (x509.CertificateBuilder()
        .subject_name(name("Unii DeepSeek Local CA"))
        .issuer_name(name("Unii DeepSeek Local CA"))
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

def make_leaf():
    if os.path.exists(LEAF_CERT_PATH) and os.path.exists(LEAF_KEY_PATH):
        return
    ca_key = serialization.load_pem_private_key(open(CA_KEY_PATH, "rb").read(), None)
    ca_cert = x509.load_pem_x509_certificate(open(CA_CERT_PATH, "rb").read())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    cert = (x509.CertificateBuilder()
        .subject_name(name("api.anthropic.com"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("api.anthropic.com")]), critical=False)
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

make_ca(); make_leaf()
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
        obj["model"] = MODEL_MAP.get(obj["model"], obj["model"])
    tools = obj.get("tools")
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict) and isinstance(t.get("type"), str) and t["type"].startswith("web_search_"):
                t["type"] = "web_search_20260209"
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()

def relay(src, dst):
    while True:
        b = src.recv(65536)
        if not b:
            break
        dst.sendall(b)

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
    conn.sendall(b"HTTP/1.1 403 Blocked by Unii DeepSeek proxy\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")

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
    with socket.create_connection((host, port), timeout=30) as up:
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
    with socket.create_connection((host, port), timeout=15) as up:
        up.settimeout(None)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        t = threading.Thread(target=copy, args=(up, conn), daemon=True)
        t.start()
        copy(conn, up)
        t.join(timeout=5)

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
    log(f"MODEL-RQ path={target} bytes={len(newbody)} -> https://{UPSTREAM}/anthropic{target}")
    out_headers = []
    for k, v in headers:
        if k in ("host", "content-length", "connection", "proxy-connection", "keep-alive",
                 "transfer-encoding", "expect", "x-api-key", "anthropic-beta", "authorization"):
            continue
        out_headers.append((k, v))
    upstream_req = (
        f"POST /anthropic{target} HTTP/1.1\r\n"
        f"Host: {UPSTREAM}\r\n"
        f"Authorization: Bearer {os.environ['DEEPSEEK_API_KEY']}\r\n"
        f"content-type: application/json\r\n"
        f"anthropic-version: 2023-06-01\r\n"
        f"content-length: {len(newbody)}\r\n"
        f"connection: close\r\n\r\n"
    ).encode("latin1") + newbody
    with socket.create_connection((UPSTREAM, 443), timeout=30) as plain:
        with upstream_tls.wrap_socket(plain, server_hostname=UPSTREAM) as up:
            up.settimeout(None)
            up.sendall(upstream_req)
            resph = recv_until_headers(up)
            if not resph:
                return False
            log("MODEL-RS", resph.split(b"\r\n", 1)[0].decode("latin1"))
            client.sendall(resph)
            relay(up, client)
    return True

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
        log(f"ALLOW CONNECT {target} (intercept; upstream={UPSTREAM})")
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
        except: pass

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(LISTEN)
s.listen(128)
log(f"listening on http://{LISTEN[0]}:{LISTEN[1]}; intercept={sorted(HIJACK_HOSTS)}; upstream=https://{UPSTREAM}/anthropic")
while True:
    c, a = s.accept()
    threading.Thread(target=handle, args=(c,a), daemon=True).start()

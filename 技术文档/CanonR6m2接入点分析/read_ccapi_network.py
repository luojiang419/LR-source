"""仅 GET 读取用户指定相机的 CCAPI 表及其公布的网络接口；不修改网络设置。"""

import argparse
import hashlib
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ALLOWED = re.compile(
    r"/ccapi/ver\d+/(?:deviceinformation|functions/wifisetting(?:/set[1-3])?"
    r"|functions/networkfunctionmode|functions/networksetting/(?:currentconnectionsetting"
    r"|connectionsetting(?:/set\d+)?|commsetting(?:/comm\d+)?|functionsetting(?:/mode\d+)?))"
)
SENSITIVE = re.compile(r"password|passphrase|secret|token|private.?key|serialnumber|macaddress|^guid$", re.I)


def redact(value):
    if isinstance(value, dict):
        return {key: "<redacted>" if SENSITIVE.search(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect refused; use the camera's explicit endpoint")


def api_paths(table):
    if not isinstance(table, dict):
        raise ValueError("API table must be an object")
    found = set()
    for version, entries in table.items():
        if not re.fullmatch(r"ver\d+", version) or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("get") is not True:
                continue
            path = entry.get("path")
            if isinstance(path, str) and ALLOWED.fullmatch(path):
                found.add(path)
    return sorted(found, key=lambda value: ("deviceinformation" not in value, value))


def collect(endpoint, *, tls_cert=None, tls_cert_sha256=None):
    url = urllib.parse.urlsplit(endpoint)
    if url.scheme not in {"http", "https"} or not url.hostname:
        raise ValueError("provide an explicit http:// or https:// camera address")
    if url.username or url.password or url.query or url.fragment or url.path not in {"", "/", "/ccapi", "/ccapi/"}:
        raise ValueError("provide the camera origin or /ccapi URL without credentials or query parameters")
    origin = urllib.parse.urlunsplit((url.scheme, url.netloc, "", "", ""))
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    certificate_digest = None
    if tls_cert is not None or tls_cert_sha256 is not None:
        if url.scheme != "https" or tls_cert is None or tls_cert_sha256 is None:
            raise ValueError("HTTPS certificate and its SHA-256 must be supplied together")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", tls_cert_sha256):
            raise ValueError("certificate SHA-256 must contain 64 hexadecimal characters")
        pem = Path(tls_cert).read_text(encoding="ascii")
        if pem.count("-----BEGIN CERTIFICATE-----") != 1:
            raise ValueError("provide exactly one camera certificate")
        der = ssl.PEM_cert_to_DER_cert(pem)
        certificate_digest = hashlib.sha256(der).hexdigest()
        if certificate_digest != tls_cert_sha256.lower():
            raise ValueError("camera certificate SHA-256 mismatch")
        # 独立信任上下文；仍校验有效期、主机名/IP 和证书链，不修改系统证书库。
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem)
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    report = {"observed_at": datetime.now(timezone.utc).isoformat(), "endpoint": origin, "scope": "GET only; no discovery scan or settings writes", "responses": {}}
    if certificate_digest:
        report["tls"] = {"trust": "explicit single camera certificate; process-local", "certificate_sha256": certificate_digest, "hostname_verification": True}

    def get(path):
        request = urllib.request.Request(origin + path, method="GET", headers={"Accept": "application/json"})
        with opener.open(request, timeout=8) as response:
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise ValueError("JSON response exceeded 1 MiB")
            return json.loads(payload)

    table = get("/ccapi")
    paths = api_paths(table)
    report["api_table"] = redact(table)
    report["advertised_readable_paths"] = paths
    report["requested_paths"] = paths[:32]
    report["omitted_paths"] = max(0, len(paths) - 32)
    for path in paths[:32]:
        try:
            report["responses"][path] = {"status": "ok", "data": redact(get(path))}
        except urllib.error.HTTPError as error:
            report["responses"][path] = {"status": error.code, "authentication_required": error.code == 401}
        except (OSError, ValueError) as error:
            report["responses"][path] = {"status": "failed", "error": str(error)}
    report["limitation"] = "Advertised APIs and stored CCAPI settings do not by themselves prove EOS Utility default selection behavior."
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tls-cert", type=Path, help="本次连接明确选定的相机 PEM 证书；不修改系统信任")
    parser.add_argument("--tls-cert-sha256", help="与 PEM 配套的 DER 证书 SHA-256")
    args = parser.parse_args()
    try:
        result = collect(args.endpoint, tls_cert=args.tls_cert, tls_cert_sha256=args.tls_cert_sha256)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Read-only CCAPI probe failed: {error}")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Saved redacted report:", args.output.resolve())

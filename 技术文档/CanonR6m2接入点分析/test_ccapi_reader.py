"""在本地模拟相机验证探测器只执行允许的 GET，且不泄露凭据；不代替实机测试。"""

import json
import hashlib
import ipaddress
import ssl
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError

from read_ccapi_network import collect


class ReaderContractTest(unittest.TestCase):
    def test_https_camera_certificate_trust_and_negative_controls(self):
        # 使用临时本地相机证书，验证信任边界；不读取真实机身或系统证书库。
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SIMULATED CAMERA")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append((self.command, self.path))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ver100": []}')

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as folder:
            cert_file, key_file = Path(folder) / "camera.pem", Path(folder) / "key.pem"
            cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_file, key_file)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            endpoint = f"https://127.0.0.1:{server.server_port}"
            try:
                result = collect(endpoint, tls_cert=cert_file, tls_cert_sha256=digest)
                self.assertTrue(result["tls"]["hostname_verification"])
                self.assertEqual(result["tls"]["certificate_sha256"], digest)
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    collect(endpoint, tls_cert=cert_file, tls_cert_sha256="0" * 64)
                with self.assertRaises(URLError):
                    collect(endpoint)
                with self.assertRaises(URLError):
                    collect(f"https://localhost:{server.server_port}", tls_cert=cert_file, tls_cert_sha256=digest)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
        self.assertEqual(observed, [("GET", "/ccapi")])

    def test_get_only_advertised_paths_and_redaction(self):
        observed = []
        table = {"ver100": [
            {"path": "/ccapi/ver100/functions/wifisetting/set1", "get": True, "put": True},
            {"path": "/ccapi/ver100/deviceinformation", "get": True},
            {"path": "/ccapi/ver100/functions/wifisetting/set2", "get": False, "put": True},
            {"path": "http://unrelated.invalid/ccapi/ver100/deviceinformation", "get": True},
            {"path": "/ccapi/ver100/functions/wifisetting/set3?write=1", "get": True},
        ]}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append((self.command, self.path))
                if self.path == "/ccapi":
                    value = table
                elif self.path.endswith("deviceinformation"):
                    value = {"productname": "SIMULATED CAMERA", "serialnumber": "private-serial"}
                else:
                    value = {"method": "cameraap", "password": "private-password"}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = collect(f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
        self.assertEqual(observed, [
            ("GET", "/ccapi"),
            ("GET", "/ccapi/ver100/deviceinformation"),
            ("GET", "/ccapi/ver100/functions/wifisetting/set1"),
        ])
        output = json.dumps(result)
        self.assertNotIn("private-password", output)
        self.assertNotIn("private-serial", output)
        self.assertIn("cameraap", output)


if __name__ == "__main__":
    unittest.main()

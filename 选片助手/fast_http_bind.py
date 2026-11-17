"""本进程 HTTP 服务绑定端口时不做反向 DNS。

标准库 HTTPServer.server_bind 会调用 socket.getfqdn(host)。在部分 Windows
网络环境下（0.0.0.0、NetBIOS/DNS 超时）该调用可阻塞十几秒，拖住启动主线程。
本项目不使用 server_name 生成任何地址，直接使用绑定主机名即可。
"""
import http.server
import socketserver

_original=http.server.HTTPServer.server_bind


def _server_bind(self):
    socketserver.TCPServer.server_bind(self)
    host,port=self.server_address[:2]
    self.server_name=str(host)
    self.server_port=port


def install():
    if http.server.HTTPServer.server_bind is not _server_bind:
        http.server.HTTPServer.server_bind=_server_bind


install()

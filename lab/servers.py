"""Servers that run in the ISP namespace: UDP echo (many ports) + HTTP bytes server.

UDP echo is what the call/game generators talk to. It binds to several ports,
including deliberately *unusual* ones, so the rule baseline cannot know them.
HTTP /bytes/N returns N bytes (video segments, web objects).
"""
import selectors
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ECHO_PORTS = [3478, 7777, 44444, 51234, 40000, 40001, 40002, 40003, 40004]
HTTP_PORT = 8000


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        try:
            n = int(self.path.rsplit("/", 1)[-1])
        except ValueError:
            n = 1000
        n = max(0, min(n, 50_000_000))
        self.send_response(200)
        self.send_header("Content-Length", str(n))
        self.end_headers()
        chunk = b"x" * 65536
        while n > 0:
            self.wfile.write(chunk[:n]); n -= len(chunk)

    def log_message(self, *a):
        pass


def udp_echo():
    """Echo each datagram back *from the address it was sent to*. A plain recvfrom/sendto on a
    wildcard socket answers from the interface's own address, which breaks NAT (reply is not
    recognised by conntrack, the flow stays UNREPLIED). IP(V6)_PKTINFO keeps the source right."""
    sel = selectors.DefaultSelector()
    for fam, bind in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        for p in ECHO_PORTS:
            try:
                s = socket.socket(fam, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if fam == socket.AF_INET:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)
                else:
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVPKTINFO, 1)
                s.bind((bind, p)); s.setblocking(False)
            except OSError:      # e.g. no IPv6 in this environment: skip that family
                break
            sel.register(s, selectors.EVENT_READ)
    while True:
        for key, _ in sel.select():
            sock = key.fileobj
            try:
                data, anc, _flags, addr = sock.recvmsg(2048, 256)
                reply_anc = []
                for level, typ, cdata in anc:
                    if level == socket.IPPROTO_IP and typ == socket.IP_PKTINFO:
                        _idx, _spec, dst = struct.unpack("I4s4s", cdata[:12])
                        reply_anc.append((socket.IPPROTO_IP, socket.IP_PKTINFO, struct.pack("I4s4s", 0, dst, b"\0" * 4)))
                    elif level == socket.IPPROTO_IPV6 and typ == socket.IPV6_PKTINFO:
                        dst6 = cdata[:16]
                        reply_anc.append((socket.IPPROTO_IPV6, socket.IPV6_PKTINFO, dst6 + struct.pack("I", 0)))
                sock.sendmsg([data], reply_anc, 0, addr)
            except OSError:
                pass


if __name__ == "__main__":
    threading.Thread(target=udp_echo, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), H).serve_forever()

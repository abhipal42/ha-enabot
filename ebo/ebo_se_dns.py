#!/usr/bin/env python3
"""
ebo_se_dns.py — a tiny UDP DNS forwarder on 127.0.0.1:53 for the EBO SE native bridge.

Why this exists: the SE's native TUTK libraries run on the Android (bionic) libc we ship in the
bundle. That libc resolves DNS via Android's netd / system-properties, neither of which exists in
this add-on container — so TUTK's master-server lookup fails with IOTC_ER_FAIL_RESOLVE_HOSTNAME
(-2). With ANDROID_DNS_MODE=local the bionic resolver falls back to the classic BSD `res_*` path,
which — when no nameservers are configured — queries 127.0.0.1:53. So we run a forwarder there and
relay every query to the container's REAL resolver (which glibc/Python use fine), closing the gap.

Only started when an EBO SE is configured. Upstream is taken from /etc/resolv.conf (then a couple
of well-known fallbacks). Pure stdlib; forwards raw DNS packets both ways.
"""
import os
import socket
import struct
import threading

LISTEN = ("127.0.0.1", 53)


def _upstreams():
    """Ordered list of (ip, 53) upstream resolvers: /etc/resolv.conf first, then common fallbacks.
    Skip 127.0.0.1 itself so we never forward to ourselves."""
    out, seen = [], set()
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver"):
                    ip = line.split()[1]
                    if ":" not in ip and ip != "127.0.0.1" and ip not in seen:
                        seen.add(ip)
                        out.append(ip)
    except Exception:
        pass
    for ip in ("127.0.0.11", "172.30.32.3", "1.1.1.1", "8.8.8.8"):  # docker DNS, supervisor DNS, public
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


UPSTREAMS = _upstreams()


def _resolve(query: bytes) -> bytes:
    for ip in UPSTREAMS:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(3)
            s.sendto(query, (ip, 53))
            resp, _ = s.recvfrom(4096)
            s.close()
            if resp:
                return resp
        except Exception:
            continue
    return b""


def _serve_one(sock, data, addr):
    resp = _resolve(data)
    if resp:
        try:
            sock.sendto(resp, addr)
        except Exception:
            pass


def main():
    try:
        from ebo_log import log
    except Exception:
        def log(*a, **k):
            print(*a)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(LISTEN)
    except OSError as e:
        log("[se-dns] could not bind %s:%d (%s) — SE DNS forwarder not started" % (LISTEN[0], LISTEN[1], e))
        return
    log("[se-dns] forwarding 127.0.0.1:53 -> %s" % ", ".join(UPSTREAMS))
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except Exception:
            continue
        threading.Thread(target=_serve_one, args=(sock, data, addr), daemon=True).start()


if __name__ == "__main__":
    main()

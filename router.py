from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from typing import Optional

MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
PORT = int(os.getenv("PORT", "5000"))
UPDATE_INTERVAL = float(os.getenv("UPDATE_INTERVAL", "5"))
ROUTE_TIMEOUT = float(os.getenv("ROUTE_TIMEOUT", "18"))
INFINITY = int(os.getenv("INFINITY", "16"))

# routing_table: subnet -> {"dist": int, "next": str | None, "learned_via": str | None}
# Direct routes: next "0.0.0.0", learned_via None
routing_table = {}
_route_lock = threading.Lock()
_last_heard = {}  # neighbor router_id -> timestamp


def enable_router_forwarding():
    """Linux must forward; rp_filter off avoids drops on multi-homed containers."""
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
            f.write("1")
    except OSError:
        pass
    conf = "/proc/sys/net/ipv4/conf"
    try:
        for name in os.listdir(conf):
            path = os.path.join(conf, name, "rp_filter")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "w") as f:
                    f.write("0")
            except OSError:
                pass
    except OSError:
        pass


def _run_ip(args):
    return subprocess.run(
        ["ip", *args], capture_output=True, text=True, check=False
    )


def discover_connected_subnets():
    """Return subnets this host is on (kernel 'scope link' connected routes)."""
    out = _run_ip(["-j", "route", "list", "scope", "link", "proto", "kernel"])
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        routes = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    subnets = []
    for r in routes:
        dst = r.get("dst")
        if not dst:
            continue
        if "/" not in dst:
            plen = int(r.get("prefixlen", 32))
            if plen >= 32:
                continue
            # rare: point-to-point without CIDR string
            continue
        subnets.append(dst)
    return subnets


def refresh_direct_routes():
    """Distance 0 to locally connected subnets."""
    subs = discover_connected_subnets()
    if not subs:
        return
    with _route_lock:
        for s in subs:
            routing_table[s] = {"dist": 0, "next": "0.0.0.0", "learned_via": None}


def src_ip_for_dst(dst_ip: str) -> str:
    """Local IP the kernel would use to reach dst_ip (DV router_id on that link)."""
    out = _run_ip(["route", "get", dst_ip])
    if out.returncode != 0:
        return MY_IP
    m = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", out.stdout)
    return m.group(1) if m else MY_IP


def apply_kernel_route(subnet: str, via_ip: Optional[str], dist: int):
    """Install remote routes only; leave connected subnets to the kernel."""
    if dist <= 0:
        return
    if via_ip is None or via_ip == "0.0.0.0":
        return
    if dist >= INFINITY:
        remove_kernel_route(subnet)
        return
    os.system(f"ip route replace {subnet} via {via_ip}")


def remove_kernel_route(subnet: str):
    os.system(f"ip route del {subnet} 2>/dev/null")


def install_routes_from_table():
    with _route_lock:
        snap = dict(routing_table)
    for subnet, info in snap.items():
        apply_kernel_route(subnet, info.get("next"), info.get("dist", INFINITY))


def routes_for_neighbor(neighbor: str) -> list[dict]:
    """Split horizon: omit routes learned via this neighbor."""
    out = []
    with _route_lock:
        for subnet, info in routing_table.items():
            learned = info.get("learned_via")
            if learned and learned == neighbor:
                continue
            out.append({"subnet": subnet, "distance": info["dist"]})
    return out


def broadcast_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while True:
            for nb in NEIGHBORS:
                rid = src_ip_for_dst(nb)
                routes = routes_for_neighbor(nb)
                pkt = {"router_id": rid, "version": 1.0, "routes": routes}
                try:
                    sock.sendto(
                        json.dumps(pkt).encode("utf-8"), (nb, PORT)
                    )
                except OSError:
                    pass
            time.sleep(UPDATE_INTERVAL)
    finally:
        sock.close()


def expire_stale_routes(now: float):
    """Drop routes from neighbors we have not heard from (node/link down)."""
    dead = [
        n
        for n, t in list(_last_heard.items())
        if now - t > ROUTE_TIMEOUT
    ]
    if not dead:
        return
    changed = False
    with _route_lock:
        for neighbor_id in dead:
            to_del = [
                s
                for s, info in routing_table.items()
                if info.get("learned_via") == neighbor_id
            ]
            for s in to_del:
                del routing_table[s]
                remove_kernel_route(s)
                changed = True
            _last_heard.pop(neighbor_id, None)
    if changed:
        install_routes_from_table()


def update_logic(neighbor_ip: str, routes_from_neighbor: list):
    """Bellman-Ford: relax edges (neighbor -> subnet) with cost +1."""
    now = time.time()
    _last_heard[neighbor_ip] = now

    with _route_lock:
        for entry in routes_from_neighbor:
            if not isinstance(entry, dict):
                continue
            subnet = entry.get("subnet")
            if not subnet or "/" not in str(subnet):
                continue
            try:
                d = int(entry.get("distance", INFINITY))
            except (TypeError, ValueError):
                continue
            cand = min(d + 1, INFINITY)
            cur = routing_table.get(subnet)
            if cur and cur.get("dist") == 0 and cur.get("next") == "0.0.0.0":
                continue
            if cand >= INFINITY:
                if cur and cur.get("learned_via") == neighbor_ip:
                    del routing_table[subnet]
                    remove_kernel_route(subnet)
                continue
            if cur is None or cand < cur["dist"]:
                routing_table[subnet] = {
                    "dist": cand,
                    "next": neighbor_ip,
                    "learned_via": neighbor_ip,
                }
            elif cur.get("learned_via") == neighbor_ip:
                routing_table[subnet] = {
                    "dist": cand,
                    "next": neighbor_ip,
                    "learned_via": neighbor_ip,
                }

    install_routes_from_table()


def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(1.0)
    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            expire_stale_routes(time.time())
            continue
        except OSError:
            time.sleep(0.2)
            continue
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if float(msg.get("version", 0)) != 1.0:
            continue
        rid = msg.get("router_id")
        if not rid or not isinstance(rid, str):
            continue
        routes = msg.get("routes")
        if not isinstance(routes, list):
            continue
        update_logic(rid, routes)
        expire_stale_routes(time.time())


def periodic_refresh_direct():
    while True:
        time.sleep(UPDATE_INTERVAL)
        refresh_direct_routes()


if __name__ == "__main__":
    enable_router_forwarding()
    refresh_direct_routes()
    install_routes_from_table()
    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=periodic_refresh_direct, daemon=True).start()
    listen_for_updates()

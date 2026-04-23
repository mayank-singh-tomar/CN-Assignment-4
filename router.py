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

routing_table = {}
_route_lock = threading.Lock()
_last_heard = {}


# -------------------- SYSTEM SETUP --------------------

def enable_router_forwarding():
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
    return subprocess.run(["ip", *args], capture_output=True, text=True, check=False)


# -------------------- SUBNET HANDLING --------------------

def discover_connected_subnets():
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
        if dst and "/" in dst:
            subnets.append(dst)
    return subnets


def refresh_direct_routes():
    subs = discover_connected_subnets()
    with _route_lock:
        for s in subs:
            routing_table[s] = {"dist": 0, "next": "0.0.0.0", "learned_via": None}


def add_fake_subnet():
    """IMPORTANT: simulate unique network per router"""
    last_octet = MY_IP.split(".")[-1]
    fake_subnet = f"192.168.{last_octet}.0/24"

    with _route_lock:
        routing_table[fake_subnet] = {
            "dist": 0,
            "next": "0.0.0.0",
            "learned_via": None,
        }

    print(f"[INIT] Local subnet added: {fake_subnet}")


# -------------------- ROUTE MANAGEMENT --------------------

def src_ip_for_dst(dst_ip: str) -> str:
    out = _run_ip(["route", "get", dst_ip])
    if out.returncode != 0:
        return MY_IP
    m = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", out.stdout)
    return m.group(1) if m else MY_IP


def apply_kernel_route(subnet: str, via_ip: Optional[str], dist: int):
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


def routes_for_neighbor(neighbor: str):
    routes = []

    with _route_lock:
        for subnet, info in routing_table.items():

            # Split Horizon Rule
            # Do NOT advertise routes back to the neighbor
            # from which they were learned
            if info.get("learned_via") == neighbor:
                continue

            # Also skip unreachable routes
            if info.get("dist", INFINITY) >= INFINITY:
                continue

            routes.append({
                "subnet": subnet,
                "distance": info["dist"]
            })

    return routes


def broadcast_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        while True:
            for nb in NEIGHBORS:
                rid = src_ip_for_dst(nb)
                routes = routes_for_neighbor(nb)

                print(f"[SEND] {MY_IP} -> {nb}: {routes}")

                pkt = {
                    "router_id": rid,
                    "version": 1.0,
                    "routes": routes,
                }

                try:
                    sock.sendto(json.dumps(pkt).encode(), (nb, PORT))
                except OSError:
                    pass

            time.sleep(UPDATE_INTERVAL)
    finally:
        sock.close()


def update_logic(neighbor_ip: str, routes_from_neighbor: list):
    print(f"[RECEIVED] from {neighbor_ip}: {routes_from_neighbor}")

    _last_heard[neighbor_ip] = time.time()

    with _route_lock:
        for entry in routes_from_neighbor:
            subnet = entry.get("subnet")
            if not subnet:
                continue

            try:
                d = int(entry.get("distance", INFINITY))
            except:
                continue

            cand = min(d + 1, INFINITY)
            cur = routing_table.get(subnet)

            if cur and cur.get("dist") == 0:
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

    print(f"[TABLE UPDATED] {routing_table}")
    install_routes_from_table()


def expire_stale_routes(now: float):
    dead = [n for n, t in _last_heard.items() if now - t > ROUTE_TIMEOUT]

    with _route_lock:
        for n in dead:
            print(f"[TIMEOUT] Removing routes via {n}")

            to_del = [
                s for s, info in routing_table.items()
                if info.get("learned_via") == n
            ]

            for s in to_del:
                del routing_table[s]
                remove_kernel_route(s)

            _last_heard.pop(n, None)


# -------------------- LISTENER --------------------

def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(1.0)

    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            expire_stale_routes(time.time())
            continue

        try:
            msg = json.loads(data.decode())
        except:
            continue

        rid = msg.get("router_id")
        routes = msg.get("routes")

        if rid and isinstance(routes, list):
            update_logic(rid, routes)



if __name__ == "__main__":
    print(f"[START] Router started with IP {MY_IP}")

    enable_router_forwarding()
    refresh_direct_routes()
    add_fake_subnet()   
    install_routes_from_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=listen_for_updates, daemon=True).start()

    while True:
        time.sleep(UPDATE_INTERVAL)

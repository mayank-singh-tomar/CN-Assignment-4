from __future__ import annotations
import ipaddress
import json
import os
import socket
import subprocess
import threading
import time

MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
PORT = int(os.getenv("PORT", "5000"))


UPDATE_INTERVAL = 1        
ROUTE_TIMEOUT = 15       
INFINITY = 16

routing_table = {}
_last_heard = {}
_lock = threading.Lock()
LOCAL_SUBNETS = set()

# ---------------- INIT ----------------

def discover_local_subnets():
    subnets = set()
    try:
        res = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            check=False,
            capture_output=True,
            text=True,
        )
        for line in res.stdout.splitlines():
            parts = line.split()
            if "inet" not in parts:
                continue
            cidr = parts[parts.index("inet") + 1]
            iface = ipaddress.ip_interface(cidr)
            net = str(iface.network)
            if net.startswith("10.0."):
                subnets.add(net)
    except Exception:
        pass
    return subnets


def reconcile_kernel_routes():
    with _lock:
        entries = dict(routing_table)

    for subnet, info in entries.items():
        if subnet in LOCAL_SUBNETS:
            continue

        if info.get("dist", INFINITY) >= INFINITY or not info.get("via"):
            subprocess.run(["ip", "route", "del", subnet], check=False, capture_output=True)
            continue

        subprocess.run(
            ["ip", "route", "replace", subnet, "via", info["via"], "metric", str(info["dist"])],
            check=False,
            capture_output=True,
        )


def refresh_local_subnets():
    current = discover_local_subnets()
    changed = False

    with _lock:
        added = current - LOCAL_SUBNETS
        removed = LOCAL_SUBNETS - current

        for subnet in added:
            routing_table[subnet] = {
                "dist": 0,
                "next": "0.0.0.0",
                "via": None,
            }
            changed = True

        for subnet in removed:
            # If a local interface disappears, stop advertising it as directly connected.
            if subnet in routing_table and routing_table[subnet].get("dist") == 0:
                del routing_table[subnet]
                changed = True

        LOCAL_SUBNETS.clear()
        LOCAL_SUBNETS.update(current)

    if changed:
        print(f"[LOCAL] {MY_IP} subnets: {sorted(LOCAL_SUBNETS)}")
        reconcile_kernel_routes()


def add_local_subnet():
    refresh_local_subnets()
    print(f"[INIT] {MY_IP} started")

# ---------------- SEND ----------------

def routes_for_neighbor(nb):
    out = []
    with _lock:
        for s, info in routing_table.items():

            # 🔥 POISON REVERSE
            if info["via"] == nb:
                out.append({"subnet": s, "distance": INFINITY})
            else:
                out.append({"subnet": s, "distance": info["dist"]})

    return out


def local_ip_for_neighbor(nb):
    """Resolve the local source IP used to reach a specific neighbor."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((nb, PORT))
        return s.getsockname()[0]
    except Exception:
        return MY_IP
    finally:
        s.close()


def send_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    while True:
        refresh_local_subnets()

        for nb in NEIGHBORS:
            pkt = {
                "router_id": MY_IP,
                "routes": routes_for_neighbor(nb)
            }

            try:
                sock.sendto(json.dumps(pkt).encode(), (nb, PORT))
            except:
                pass

        time.sleep(UPDATE_INTERVAL)

# ---------------- RECEIVE ----------------

def update_logic(neighbor_ip, routes):
    _last_heard[neighbor_ip] = time.time()

    updated = False

    with _lock:
        for r in routes:
            s = r["subnet"]
            d = int(r["distance"])

            new_dist = min(d + 1, INFINITY)
            cur = routing_table.get(s)

            # skip own
            if cur and cur["dist"] == 0:
                continue

            # poison
            if new_dist >= INFINITY:
                if cur and cur["via"] == neighbor_ip:
                    cur["dist"] = INFINITY
                    cur["next"] = None
                    cur["via"] = None
                    updated = True
                continue

            # new or better
            if cur is None or new_dist < cur["dist"]:
                routing_table[s] = {
                    "dist": new_dist,
                    "next": neighbor_ip,
                    "via": neighbor_ip,
                }
                updated = True

            # update same path - always accept updates from current path
            elif cur["via"] == neighbor_ip:
                old_dist = cur["dist"]
                cur["dist"] = new_dist
                if old_dist != new_dist:
                    updated = True

    if updated:
        print(f"[TABLE] {routing_table}")
        reconcile_kernel_routes()

# ---------------- TIMEOUT ----------------

def expire_routes():
    now = time.time()
    updated = False

    with _lock:
        for nb, t in list(_last_heard.items()):
            if now - t > ROUTE_TIMEOUT:
                print(f"[TIMEOUT] {nb} dead")

                for s, info in routing_table.items():
                    if info["via"] == nb:
                        info["dist"] = INFINITY
                        info["next"] = None
                        info["via"] = None
                        updated = True

                del _last_heard[nb]

    if updated:
        reconcile_kernel_routes()

# ---------------- LISTENER ----------------

def listen():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(1)

    while True:
        try:
            data, (src_ip, _) = sock.recvfrom(65535)
            msg = json.loads(data.decode())
            advertised_ip = msg.get("router_id", src_ip)
            neighbor_ip = advertised_ip if advertised_ip in NEIGHBORS else src_ip
            update_logic(neighbor_ip, msg["routes"])
        except socket.timeout:
            expire_routes()
        except:
            pass

# ---------------- MAIN ----------------

if __name__ == "__main__":
    print(f"[START] {MY_IP}")

    add_local_subnet()

    threading.Thread(target=send_updates, daemon=True).start()
    threading.Thread(target=listen, daemon=True).start()

    while True:
        time.sleep(5)

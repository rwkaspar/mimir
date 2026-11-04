#!/usr/bin/env python3
"""Minimal WiFi setup server for first-boot (stdlib only, no pip packages).

Starts a hotspot, serves setup.html, handles WiFi scan/connect via nmcli.
Exits when internet connectivity is established (WiFi configured or Ethernet).
Must run as root.
"""

import http.server
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

HOTSPOT_SSID = "mimir-Setup"
HOTSPOT_PASSWORD = "photoframe"
PORT = 80
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
VIEWER_DIR = os.path.join(REPO_DIR, "viewer")


def run_cmd(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), 1


def has_internet():
    _, rc = run_cmd(["ping", "-c", "1", "-W", "3", "8.8.8.8"], timeout=5)
    return rc == 0


def get_gateway_ip():
    """Get the Pi's IP on the hotspot interface (usually 10.42.0.1)."""
    out, _ = run_cmd(["ip", "-4", "-o", "addr", "show"])
    for line in out.split("\n"):
        # Look for 10.42.x.x (nmcli hotspot default range)
        parts = line.split()
        for p in parts:
            if p.startswith("10.42."):
                return p.split("/")[0]
    return "10.42.0.1"  # fallback


def start_hotspot():
    # Clear any stale hotspot profile first. Otherwise `nmcli device wifi
    # hotspot` can reuse an existing "Hotspot" connection (e.g. left over with
    # the previous SSID) instead of broadcasting the SSID we show on screen and
    # in the QR — then the phone looks for a network that isn't on the air and
    # the join fails, manually and via QR.
    for stale in ("hotspot", "Hotspot", "PhotoFrame-Setup"):
        run_cmd(["nmcli", "connection", "delete", stale], timeout=10)
    run_cmd(["nmcli", "device", "wifi", "hotspot", "con-name", "hotspot",
             "ssid", HOTSPOT_SSID, "password", HOTSPOT_PASSWORD])
    # Ground-truth log: what SSID is actually being broadcast now?
    ssid, _ = run_cmd(["nmcli", "-g", "802-11-wireless.ssid",
                       "connection", "show", "hotspot"])
    active, _ = run_cmd(["nmcli", "-t", "-f", "NAME,DEVICE,STATE",
                         "connection", "show", "--active"])
    log(f"[wifi-setup] Hotspot up: broadcasting SSID={ssid!r} (expected {HOTSPOT_SSID!r})")
    log(f"[wifi-setup] active connections: {active!r}")
    # Captive portal: redirect all HTTP to us (phones try external URLs)
    run_cmd(["iptables", "-t", "nat", "-A", "PREROUTING",
             "-p", "tcp", "--dport", "80", "-j", "REDIRECT", "--to-port", str(PORT)])


def stop_hotspot():
    run_cmd(["iptables", "-t", "nat", "-F", "PREROUTING"])  # clears both HTTP + DNS redirects
    out, _ = run_cmd(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"])
    for line in out.split("\n"):
        if "hotspot" in line.lower() or HOTSPOT_SSID in line:
            name = line.split(":")[0]
            run_cmd(["nmcli", "connection", "down", name])
            break


def scan_wifi():
    run_cmd(["nmcli", "device", "wifi", "rescan"])
    time.sleep(2)
    out, _ = run_cmd(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"])
    networks = []
    seen = set()
    for line in out.split("\n"):
        parts = line.split(":")
        if len(parts) >= 3:
            ssid = parts[0].strip()
            if not ssid or ssid in seen or ssid == HOTSPOT_SSID:
                continue
            seen.add(ssid)
            try:
                signal = int(parts[1])
            except ValueError:
                signal = 0
            networks.append({"ssid": ssid, "signal": signal, "secured": bool(parts[2].strip())})
    networks.sort(key=lambda x: -x["signal"])
    return networks


def connect_wifi(ssid, password):
    stop_hotspot()
    time.sleep(1)
    cmd = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    out, rc = run_cmd(cmd, timeout=30)
    if rc != 0:
        log(f"[wifi-setup] nmcli connect failed: {out}")
        start_hotspot()
        return False, out
    # nmcli returned success — wait for actual internet connectivity
    log(f"[wifi-setup] nmcli connected to {ssid}, verifying internet...")
    for i in range(15):
        time.sleep(2)
        if has_internet():
            log(f"[wifi-setup] Internet verified after {i+1} attempts")
            delete_hotspot_profile()
            return True, "Connected"
    # Associated but no internet (bad password, no DHCP, etc.)
    log(f"[wifi-setup] No internet after connecting to {ssid}, restarting hotspot")
    run_cmd(["nmcli", "connection", "down", ssid], timeout=10)
    start_hotspot()
    return False, "Connected to WiFi but no internet"


def delete_hotspot_profile():
    """Remove the hotspot connection profile from NetworkManager."""
    out, _ = run_cmd(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    for line in out.split("\n"):
        if "hotspot" in line.lower() or HOTSPOT_SSID in line:
            name = line.split(":")[0]
            run_cmd(["nmcli", "connection", "delete", name])
            log(f"[wifi-setup] Deleted hotspot profile: {name}")


class SetupHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"[wifi-setup] {args[0]}")

    def do_GET(self):
        # Captive portal detection URLs + main page all serve setup.html
        if self.path in ("/", "/setup", "/setup.html",
                         "/generate_204", "/hotspot-detect.html",
                         "/connecttest.txt", "/ncsi.txt"):
            self._serve_file("setup.html", "text/html")
        else:
            self._redirect("/setup")

    def do_POST(self):
        if self.path == "/api/wifi/scan":
            self._send_json({"networks": scan_wifi()})
        elif self.path == "/api/wifi/connect":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            # Respond before disconnecting hotspot
            self._send_json({"ok": True, "message": "Connecting..."})
            threading.Thread(target=self._bg_connect,
                             args=(body.get("ssid", ""), body.get("password", "")),
                             daemon=True).start()
        else:
            self.send_error(404)

    def _bg_connect(self, ssid, password):
        try:
            with open("/dev/tty1", "w") as tty:
                tty.write(f"\n\n    Connecting to {ssid}...\n")
        except Exception:
            pass
        ok, msg = connect_wifi(ssid, password)
        if ok:
            log(f"[wifi-setup] Connected to {ssid}! Exiting.")
            try:
                with open("/dev/tty1", "w") as tty:
                    tty.write(f"\n    Connected! Continuing setup...\n")
            except Exception:
                pass
            time.sleep(1)
            os._exit(0)
        else:
            log(f"[wifi-setup] Failed: {msg}")
            gateway_ip = get_gateway_ip()
            update_tty_display(gateway_ip)

    def _serve_file(self, filename, content_type):
        path = os.path.join(VIEWER_DIR, filename)
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _send_json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()


def captive_dns_server(gateway_ip, port=53):
    """Minimal DNS server that resolves ALL queries to the gateway IP.

    This makes captive portal detection work: the phone queries e.g.
    connectivitycheck.gstatic.com, gets the Pi's IP back, tries HTTP,
    and our HTTP server serves the setup page.
    """
    ip_bytes = socket.inet_aton(gateway_ip)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", port))
    except OSError as e:
        log(f"[wifi-setup] DNS server failed to bind port {port}: {e}")
        return
    log(f"[wifi-setup] DNS server on port {port}, resolving all to {gateway_ip}")
    while True:
        try:
            data, addr = sock.recvfrom(512)
            if len(data) < 12:
                continue
            # Build minimal DNS response:
            # - Copy transaction ID from query
            # - Set response flags (standard response, no error)
            # - 1 question, 1 answer, 0 authority, 0 additional
            tx_id = data[:2]
            flags = b'\x81\x80'  # standard response, recursion available
            counts = b'\x00\x01\x00\x01\x00\x00\x00\x00'
            # Question section: copy from query (starts at byte 12)
            # Find end of question: skip QNAME + QTYPE(2) + QCLASS(2)
            pos = 12
            while pos < len(data) and data[pos] != 0:
                pos += data[pos] + 1
            pos += 5  # null byte + QTYPE(2) + QCLASS(2)
            question = data[12:pos]
            # Answer: pointer to name in question (0xC00C), type A, class IN,
            # TTL 60s, data length 4, IP address
            answer = b'\xc0\x0c'  # pointer to name at offset 12
            answer += b'\x00\x01'  # type A
            answer += b'\x00\x01'  # class IN
            answer += struct.pack('>I', 60)  # TTL 60s
            answer += b'\x00\x04'  # data length
            answer += ip_bytes
            response = tx_id + flags + counts + question + answer
            sock.sendto(response, addr)
        except Exception:
            pass


def connectivity_watcher():
    """Exit if internet appears (e.g. Ethernet plugged in)."""
    while True:
        time.sleep(10)
        if has_internet():
            log("[wifi-setup] Internet detected, exiting.")
            stop_hotspot()
            os._exit(0)


def _wifi_qr_escape(s):
    """Escape SSID/password for the WIFI: QR payload (MECARD-style)."""
    for ch in ("\\", ";", ",", ":", '"'):
        s = s.replace(ch, "\\" + ch)
    return s


def wifi_qr_block():
    """Return a terminal QR code (ANSI/UTF-8) that joins the setup hotspot,
    or None if qrencode is unavailable (e.g. not installed yet)."""
    payload = "WIFI:S:%s;T:WPA;P:%s;;" % (
        _wifi_qr_escape(HOTSPOT_SSID), _wifi_qr_escape(HOTSPOT_PASSWORD))
    out, rc = run_cmd(["qrencode", "-t", "ANSIUTF8", "-m", "2", payload], timeout=10)
    if rc != 0 or not out.strip():
        return None
    return out


def update_tty_display(gateway_ip):
    """Show WiFi setup instructions (and a scannable QR) on the Pi's display."""
    qr = wifi_qr_block()
    header = f"""

        ==========================================

           Photo Frame  -  WiFi Setup

           1. Scan this QR to join the setup WiFi
              (or connect manually:
               WiFi {HOTSPOT_SSID} / {HOTSPOT_PASSWORD})
"""
    footer = f"""
           2. Your phone should open the setup page
              automatically. If not, open:

              http://{gateway_ip}

           3. Choose your home WiFi network.

           Setup will continue automatically.

        ==========================================
"""
    try:
        with open("/dev/tty1", "w") as tty:
            tty.write("\033[2J\033[H")  # clear screen + cursor home
            tty.write(header)
            if qr:
                tty.write("\n")
                tty.write(qr)
            tty.write(footer)
    except Exception as e:
        log(f"[wifi-setup] Could not write to tty1: {e}")


def log(msg):
    """Print and flush immediately (stdout may be piped through tee)."""
    print(msg, flush=True)


def main():
    log(f"[wifi-setup] Starting hotspot '{HOTSPOT_SSID}' (password: {HOTSPOT_PASSWORD})")
    start_hotspot()
    time.sleep(2)  # let hotspot interface come up

    gateway_ip = get_gateway_ip()
    log(f"[wifi-setup] Gateway IP: {gateway_ip}")

    # Show IP on Pi's display
    update_tty_display(gateway_ip)

    # Start captive portal DNS on port 5353, redirect real DNS traffic to it
    # (don't kill dnsmasq — it handles DHCP for the hotspot)
    threading.Thread(target=captive_dns_server, args=(gateway_ip, 5353), daemon=True).start()
    run_cmd(["iptables", "-t", "nat", "-A", "PREROUTING",
             "-p", "udp", "--dport", "53", "-j", "REDIRECT", "--to-port", "5353"])

    threading.Thread(target=connectivity_watcher, daemon=True).start()

    server = http.server.HTTPServer(("", PORT), SetupHandler)
    log(f"[wifi-setup] Serving setup page on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_hotspot()


if __name__ == "__main__":
    main()

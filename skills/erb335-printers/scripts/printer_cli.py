#!/usr/bin/env python3
"""UTA ERB 335 3D Printers CLI - Skynet Wi-Fi + OctoPrint + Bambu Lab LAN control.

Zero external dependencies (Python 3 standard library only).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import ctypes.wintypes as wt
import ftplib
import hashlib
import http.cookiejar
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_SSID = "Skynet"
DEFAULT_OCTO_USER = "student"
DEFAULT_OCTO_PASS = "student"

OCTOPRINT_PRINTERS = {
    "steve": "steve.local",
    "saed": "saed.local",
    "kate": "kate.local",
    "chris": "chris.local",
}
ACTIVE_OCTOPRINT = ["steve", "saed", "kate"]

# Known ERB 335 Bambu Lab serials -> default friendly name & model
KNOWN_BAMBU_SERIALS = {
    "0948AD532300868": ("h2d", "H2D"),
    "01P00C4B2900458": ("p1s-1", "P1S"),
    "01P00A442101323": ("p1s-2", "P1S/P1P"),
    "03919C450503017": ("a1", "A1"),
}

BAMBU_CONFIG_PATH = Path.home() / ".config" / "erb335-printers" / "bambu.json"


# ---------------------------------------------------------------------------
# Windows WLAN helpers (WlanScan via wlanapi.dll + netsh)
# ---------------------------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", wt.WCHAR * 256),
        ("isState", wt.DWORD),
    ]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", wt.DWORD),
        ("dwIndex", wt.DWORD),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]


def trigger_wlan_scan() -> bool:
    if os.name != "nt":
        return False
    try:
        wlanapi = ctypes.WinDLL("wlanapi.dll")
        negotiated = wt.DWORD()
        handle = wt.HANDLE()
        if wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle)) != 0:
            return False
        try:
            iface_list_ptr = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
            if wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(iface_list_ptr)) != 0 or not iface_list_ptr:
                return False
            try:
                count = iface_list_ptr.contents.dwNumberOfItems
                base_addr = ctypes.addressof(iface_list_ptr.contents.InterfaceInfo)
                for i in range(count):
                    iface = WLAN_INTERFACE_INFO.from_address(
                        base_addr + i * ctypes.sizeof(WLAN_INTERFACE_INFO)
                    )
                    wlanapi.WlanScan(handle, ctypes.byref(iface.InterfaceGuid), None, None, None)
                return count > 0
            finally:
                wlanapi.WlanFreeMemory(iface_list_ptr)
        finally:
            wlanapi.WlanCloseHandle(handle, None)
    except Exception:
        return False


def get_wifi_status() -> dict[str, str]:
    out = subprocess.check_output(["netsh", "wlan", "show", "interfaces"], text=True, errors="ignore")
    info: dict[str, str] = {}
    for key in ("State", "SSID", "Band", "Channel", "Signal", "Profile"):
        m = re.search(rf"^\s*{key}\s*:\s*(.+)$", out, re.MULTILINE)
        if m:
            info[key.lower()] = m.group(1).strip()
    return info


def wifi_connect(ssid: str = DEFAULT_SSID, timeout: int = 20) -> int:
    status = get_wifi_status()
    if status.get("state") == "connected" and status.get("ssid") == ssid:
        print(f"Already connected to '{ssid}' (Signal: {status.get('signal', '?')}, Band: {status.get('band', '?')}).")
        return 0

    print("Triggering active Wi-Fi scan (refreshing 2.4/5/6 GHz BSS cache)...")
    if trigger_wlan_scan():
        time.sleep(3)

    print(f"Connecting to '{ssid}'...")
    subprocess.run(
        ["netsh", "wlan", "connect", f"ssid={ssid}", f"name={ssid}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        st = get_wifi_status()
        if st.get("state") == "connected" and st.get("ssid") == ssid:
            print(f"Connected to '{ssid}' (Signal: {st.get('signal', '?')}, Band: {st.get('band', '?')}, Ch: {st.get('channel', '?')}).")
            return 0
        time.sleep(1)

    st = get_wifi_status()
    print(f"Failed to connect to '{ssid}' within {timeout}s (state: {st.get('state', 'unknown')}, ssid: {st.get('ssid', 'none')}).", file=sys.stderr)
    return 1


def wifi_fix_profiles() -> int:
    out = subprocess.check_output(["netsh", "wlan", "show", "profiles"], text=True, errors="ignore")
    profiles = [m.group(1).strip() for m in re.finditer(r"All User Profile\s*:\s*(.+)$", out, re.MULTILINE)]
    if "skynet" in profiles and "Skynet" in profiles:
        print("Removing duplicate lowercase profile 'skynet'...")
        tmp = Path(os.environ.get("TEMP", "."))
        subprocess.run(["netsh", "wlan", "export", "profile", "name=Skynet", f"folder={tmp}", "key=clear"], check=False, stdout=subprocess.DEVNULL)
        exported = list(tmp.glob("Wi-Fi-*Skynet*.xml")) + list(tmp.glob("Wi-Fi-*skynet*.xml"))
        xml_text = None
        for f in exported:
            content = f.read_text(encoding="utf-8", errors="ignore")
            if "<name>Skynet</name>" in content:
                xml_text = content
            f.unlink(missing_ok=True)
        if not xml_text:
            print("Could not isolate 'Skynet' XML safely; aborting.", file=sys.stderr)
            return 1
        clean_xml = tmp / "Skynet-clean.xml"
        clean_xml.write_text(xml_text, encoding="utf-8")
        try:
            subprocess.run(["netsh", "wlan", "delete", "profile", "name=skynet"], check=False, stdout=subprocess.DEVNULL)
            subprocess.run(["netsh", "wlan", "delete", "profile", "name=Skynet"], check=False, stdout=subprocess.DEVNULL)
            subprocess.run(["netsh", "wlan", "add", "profile", f"filename={clean_xml}", "user=all"], check=True, stdout=subprocess.DEVNULL)
        finally:
            clean_xml.unlink(missing_ok=True)
        print("Cleaned up duplicate 'skynet' profile; 'Skynet' preserved.")
        return 0
    print("No duplicate 'skynet' profile found.")
    return 0


# ---------------------------------------------------------------------------
# OctoPrint REST API Client
# ---------------------------------------------------------------------------

def resolve_octo_host(name: str) -> str:
    key = name.lower().removesuffix(".local")
    return OCTOPRINT_PRINTERS.get(key, name if "." in name else f"{name}.local")


class OctoPrintClient:
    def __init__(self, host: str, user: str = DEFAULT_OCTO_USER, password: str = DEFAULT_OCTO_PASS, timeout: float = 4.0):
        self.host = resolve_octo_host(host)
        self.base_url = f"http://{self.host}"
        self.user = user
        self.password = password
        self.timeout = timeout
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.csrf_token: str | None = None

    def _refresh_csrf(self) -> None:
        for c in self.cj:
            if c.name.startswith("csrf_token"):
                self.csrf_token = c.value

    def login(self) -> dict[str, Any]:
        # GET / sets the port-scoped csrf_token_P<port> cookie required by OctoPrint POSTs
        with self.opener.open(f"{self.base_url}/", timeout=self.timeout):
            pass
        self._refresh_csrf()
        data = self.request("POST", "/api/login", {"user": self.user, "pass": self.password})
        self._refresh_csrf()
        return data

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        headers: dict[str, str] = {}
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        req = urllib.request.Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        with self.opener.open(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return json.loads(raw) if raw.strip() else {}

    def upload_gcode(self, filepath: Path, select: bool = False, start_print: bool = False) -> dict[str, Any]:
        boundary = f"----OctoCLI{uuid.uuid4().hex}"
        parts: list[bytes] = []

        def add_field(name: str, value: str) -> None:
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )

        if select or start_print:
            add_field("select", "true")
        if start_print:
            add_field("print", "true")

        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filepath.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
            + filepath.read_bytes()
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token

        req = urllib.request.Request(f"{self.base_url}/api/files/local", data=b"".join(parts), headers=headers, method="POST")
        with self.opener.open(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return json.loads(raw) if raw.strip() else {}


# ---------------------------------------------------------------------------
# Bambu Lab LAN Client (MQTT-TLS :8883 + Implicit FTPS :990 + TLS Discovery)
# ---------------------------------------------------------------------------

def _bambu_studio_access_codes() -> dict[str, str]:
    candidates: list[Path] = []
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "BambuStudio" / "BambuStudio.conf")
    candidates.append(Path.home() / "Library" / "Application Support" / "BambuStudio" / "BambuStudio.conf")
    candidates.append(Path.home() / ".config" / "BambuStudio" / "BambuStudio.conf")
    for conf_path in candidates:
        if not conf_path.is_file():
            continue
        try:
            raw = conf_path.read_text(encoding="utf-8", errors="ignore").lstrip()
            conf, _ = json.JSONDecoder().raw_decode(raw)
            codes = dict(conf.get("access_code", {}))
            if codes:
                return codes
        except Exception:
            pass
    return {}


def load_bambu_config() -> dict[str, dict[str, str]]:
    printers: dict[str, dict[str, str]] = {}
    if BAMBU_CONFIG_PATH.is_file():
        try:
            data = json.loads(BAMBU_CONFIG_PATH.read_text(encoding="utf-8"))
            printers = dict(data.get("printers", {}))
        except Exception:
            pass
    # Fill any missing access codes from env or BambuStudio.conf fallback
    bs_codes = _bambu_studio_access_codes()
    for name, info in printers.items():
        serial = info.get("serial", "")
        if not info.get("access_code"):
            env_code = os.environ.get(f"BAMBU_ACCESS_CODE_{serial}")
            if env_code:
                info["access_code"] = env_code
            elif serial in bs_codes:
                info["access_code"] = bs_codes[serial]
    return printers


def save_bambu_config(printers: dict[str, dict[str, str]]) -> None:
    BAMBU_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    BAMBU_CONFIG_PATH.write_text(json.dumps({"printers": printers}, indent=2) + "\n", encoding="utf-8")


def _local_subnet_prefix() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.50.1", 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3])
    except Exception:
        pass
    return "192.168.50"


def _probe_bambu_tls_serial(ip: str, timeout: float = 0.6) -> str | None:
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((ip, 8883), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=ip) as ssock:
                der = ssock.getpeercert(binary_form=True)
                # Bambu serials are 15-char alphanumeric (e.g. 0948AD532300868, 01P00C4B2900458, 03919C450503017)
                matches = re.findall(rb"(?:00M|01P|01S|030|039|094)[A-Z0-9]{12}", der)
                if matches:
                    return matches[0].decode("ascii")
    except Exception:
        return None
    return None


def discover_bambu_printers(subnet: str | None = None) -> dict[str, str]:
    """Scan local /24 for port 8883 Bambu Lab printers. Returns {serial: ip}."""
    prefix = subnet or _local_subnet_prefix()

    def check(i: int) -> tuple[str, str] | None:
        ip = f"{prefix}.{i}"
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            if s.connect_ex((ip, 8883)) != 0:
                return None
        except Exception:
            return None
        finally:
            s.close()
        serial = _probe_bambu_tls_serial(ip, timeout=1.5)
        return (serial, ip) if serial else None

    found: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for res in ex.map(check, range(1, 255)):
            if res:
                found[res[0]] = res[1]
    return found


def _infer_bambu_model(serial: str) -> str:
    if serial in KNOWN_BAMBU_SERIALS:
        return KNOWN_BAMBU_SERIALS[serial][1]
    if serial.startswith("094"):
        return "H2D"
    if serial.startswith("01P00C"):
        return "P1S"
    if serial.startswith("01P"):
        return "P1S/P1P"
    if serial.startswith("00M"):
        return "X1C"
    if serial.startswith("039"):
        return "A1"
    if serial.startswith("030"):
        return "A1 Mini"
    return "Bambu"


def _mqtt_encode_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


def _mqtt_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def bambu_mqtt_query(ip: str, serial: str, access_code: str, timeout: float = 5.0) -> dict[str, Any]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((ip, 8883), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=ip) as s:
            s.settimeout(timeout)
            vh = _mqtt_str("MQTT") + bytes([4, 0xC2, 0, 60])
            pl = _mqtt_str(f"erb335-{uuid.uuid4().hex[:6]}") + _mqtt_str("bblp") + _mqtt_str(access_code)
            s.sendall(bytes([0x10]) + _mqtt_encode_len(len(vh) + len(pl)) + vh + pl)

            hdr = s.recv(4)
            if len(hdr) < 4 or hdr[0] != 0x20 or hdr[3] != 0:
                raise RuntimeError(f"MQTT CONNACK rejected (code={hdr[3] if len(hdr) >= 4 else '?'})")

            vh_sub = struct.pack("!H", 1)
            pl_sub = _mqtt_str(f"device/{serial}/report") + bytes([0])
            s.sendall(bytes([0x82]) + _mqtt_encode_len(len(vh_sub) + len(pl_sub)) + vh_sub + pl_sub)

            msg = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}).encode("utf-8")
            vh_pub = _mqtt_str(f"device/{serial}/request")
            s.sendall(bytes([0x30]) + _mqtt_encode_len(len(vh_pub) + len(msg)) + vh_pub + msg)

            buf = bytearray()
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while len(buf) >= 2:
                        ptype = buf[0] >> 4
                        mult, val, idx = 1, 0, 1
                        while idx < len(buf):
                            b = buf[idx]
                            val += (b & 0x7F) * mult
                            idx += 1
                            if not (b & 0x80):
                                break
                            mult *= 128
                        else:
                            break
                        if len(buf) < idx + val:
                            break
                        pkt = bytes(buf[idx : idx + val])
                        del buf[: idx + val]
                        if ptype == 3:
                            tlen = struct.unpack("!H", pkt[:2])[0]
                            payload = pkt[2 + tlen :]
                            try:
                                data = json.loads(payload.decode("utf-8", "ignore"))
                                if "print" in data and len(data["print"]) > 3:
                                    return data["print"]
                            except Exception:
                                pass
                except socket.timeout:
                    break
            raise TimeoutError("No status report received from Bambu printer")


def bambu_mqtt_command(ip: str, serial: str, access_code: str, payload_obj: dict[str, Any], timeout: float = 5.0) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((ip, 8883), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=ip) as s:
            s.settimeout(timeout)
            vh = _mqtt_str("MQTT") + bytes([4, 0xC2, 0, 60])
            pl = _mqtt_str(f"erb335-{uuid.uuid4().hex[:6]}") + _mqtt_str("bblp") + _mqtt_str(access_code)
            s.sendall(bytes([0x10]) + _mqtt_encode_len(len(vh) + len(pl)) + vh + pl)
            hdr = s.recv(4)
            if len(hdr) < 4 or hdr[0] != 0x20 or hdr[3] != 0:
                raise RuntimeError(f"MQTT CONNACK rejected (code={hdr[3] if len(hdr) >= 4 else '?'})")
            msg = json.dumps(payload_obj).encode("utf-8")
            vh_pub = _mqtt_str(f"device/{serial}/request")
            s.sendall(bytes([0x30]) + _mqtt_encode_len(len(vh_pub) + len(msg)) + vh_pub + msg)
            time.sleep(0.3)


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """Implicit FTPS (port 990) with TLS session reuse required by Bambu Lab printers."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def bambu_ftps_upload(ip: str, access_code: str, filepath: Path) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ftp = ImplicitFTP_TLS(context=ctx)
    ftp.connect(ip, 990, timeout=15)
    ftp.login("bblp", access_code)
    ftp.prot_p()
    with filepath.open("rb") as fh:
        # Bambu FTPS servers do not send TLS close_notify on data socket unwrap();
        # stream manually so we don't hang waiting for unwrap().
        ftp.voidcmd("TYPE I")
        with ftp.transfercmd(f"STOR /{filepath.name}") as conn:
            while True:
                buf = fh.read(65536)
                if not buf:
                    break
                conn.sendall(buf)
        try:
            ftp.voidresp()
        except Exception:
            pass
    try:
        ftp.quit()
    except Exception:
        ftp.close()


def bambu_wrap_gcode_to_3mf(gcode_path: Path, out_3mf: Path) -> str:
    """Wrap raw Bambu .gcode into a minimal .gcode.3mf archive with MD5 for LAN project_file start."""
    gcode_bytes = gcode_path.read_bytes()
    md5_hex = hashlib.md5(gcode_bytes).hexdigest()
    with zipfile.ZipFile(out_3mf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/plate_1.gcode", gcode_bytes)
        zf.writestr("Metadata/plate_1.gcode.md5", md5_hex)
    return md5_hex


def bambu_mqtt_start_project(
    ip: str,
    serial: str,
    access_code: str,
    remote_3mf_name: str,
    md5_hex: str = "",
    bed_type: str = "textured_plate",
    bed_leveling: bool = True,
    use_ams: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Start a .gcode.3mf file on a Bambu printer SD card via LAN MQTT project_file (requires Developer Mode)."""
    fname = remote_3mf_name.lstrip("/")
    subtask = fname.removesuffix(".gcode.3mf").removesuffix(".3mf")
    cmd = {
        "print": {
            "sequence_id": "1002",
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "url": f"file:///sdcard/{fname}",
            "subtask_name": subtask,
            "bed_type": bed_type,
            "timelapse": False,
            "bed_leveling": bed_leveling,
            "flow_cali": False,
            "vibration_cali": False,
            "layer_inspect": False,
            "use_ams": use_ams,
        }
    }
    if md5_hex:
        cmd["print"]["md5"] = md5_hex
    bambu_mqtt_command(ip, serial, access_code, cmd, timeout=timeout)
    return {}


def resolve_bambu_target(name: str, bambu_cfg: dict[str, dict[str, str]]) -> tuple[str, dict[str, str]] | None:
    key = name.lower()
    if key in bambu_cfg:
        return key, bambu_cfg[key]
    for k, v in bambu_cfg.items():
        if v.get("serial", "").lower() == key or v.get("ip") == key:
            return k, v
    return None


def ensure_bambu_ip(name: str, info: dict[str, str], bambu_cfg: dict[str, dict[str, str]]) -> str:
    ip = info.get("ip", "")
    serial = info.get("serial", "")
    if ip and _probe_bambu_tls_serial(ip, timeout=0.8) == serial:
        return ip
    # DHCP fallback: re-discover on local subnet
    found = discover_bambu_printers()
    if serial in found:
        info["ip"] = found[serial]
        bambu_cfg[name] = info
        save_bambu_config(bambu_cfg)
        return found[serial]
    return ip


def format_ams_trays(report: dict[str, Any]) -> str:
    trays: list[str] = []
    for unit in report.get("ams", {}).get("ams", []):
        for t in unit.get("tray", []):
            ttype = t.get("tray_type")
            if ttype:
                trays.append(ttype)
    return ", ".join(trays) if trays else "-"


# ---------------------------------------------------------------------------
# Subcommand Handlers
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    bambu_cfg = load_bambu_config()
    single = args.printer.lower() if args.printer else None

    show_octo = not args.bambu
    show_bambu = not args.octoprint

    if single:
        if resolve_bambu_target(single, bambu_cfg):
            show_octo = False
            show_bambu = True
        else:
            show_octo = True
            show_bambu = False

    if show_octo:
        targets = [single] if single else (list(OCTOPRINT_PRINTERS.keys()) if args.all else ACTIVE_OCTOPRINT)
        print("=== OctoPrint (Creality Ender-3 S1 Pro) ===")
        print(f"{'PRINTER':<10} {'STATE':<13} {'NOZZLE':<13} {'BED':<13} {'PROG':<7} {'FILE'}")
        print("-" * 82)
        for name in targets:
            client = OctoPrintClient(name, timeout=3.5)
            try:
                client.login()
                conn = client.request("GET", "/api/connection")
                job = client.request("GET", "/api/job")
                state = conn.get("current", {}).get("state", "Unknown")
                nozzle_s, bed_s = "-", "-"
                if state != "Closed":
                    try:
                        pr = client.request("GET", "/api/printer")
                        temps = pr.get("temperature", {})
                        t0, bed = temps.get("tool0", {}), temps.get("bed", {})
                        if t0:
                            nozzle_s = f"{t0.get('actual', 0):.1f}/{t0.get('target', 0):.0f}C"
                        if bed:
                            bed_s = f"{bed.get('actual', 0):.1f}/{bed.get('target', 0):.0f}C"
                    except urllib.error.HTTPError:
                        pass
                prog = job.get("progress", {}).get("completion")
                prog_s = f"{prog:.0f}%" if isinstance(prog, (int, float)) else "-"
                fname = job.get("job", {}).get("file", {}).get("name") or "-"
                print(f"{name:<10} {state:<13} {nozzle_s:<13} {bed_s:<13} {prog_s:<7} {fname}")
            except Exception as exc:
                print(f"{name:<10} {'UNREACHABLE':<13} {'-':<13} {'-':<13} {'-':<7} ({exc})")

    if show_bambu:
        if show_octo:
            print()
        print("=== Bambu Lab (LAN MQTT :8883) ===")
        if not bambu_cfg:
            print("No Bambu printers configured yet. Run: erb335-printers bambu sync-codes")
            return 0
        print(f"{'PRINTER':<10} {'MODEL':<8} {'STATE':<10} {'NOZZLE':<12} {'BED':<12} {'PROG':<7} {'AMS TRAYS':<24} {'FILE'}")
        print("-" * 108)
        items = [resolve_bambu_target(single, bambu_cfg)] if single else list(bambu_cfg.items())
        for item in items:
            if not item:
                continue
            name, info = item
            serial = info.get("serial", "")
            code = info.get("access_code", "")
            model = info.get("model", "Bambu")
            if not code:
                print(f"{name:<10} {model:<8} {'NO CODE':<10} {'-':<12} {'-':<12} {'-':<7} {'-':<24} (run bambu sync-codes)")
                continue
            ip = ensure_bambu_ip(name, info, bambu_cfg)
            try:
                rep = bambu_mqtt_query(ip, serial, code, timeout=4.5)
                state = rep.get("gcode_state", "?")
                noz = f"{rep.get('nozzle_temper', 0):.1f}/{rep.get('nozzle_target_temper', 0):.0f}C"
                bed = f"{rep.get('bed_temper', 0):.1f}/{rep.get('bed_target_temper', 0):.0f}C"
                pct = rep.get("mc_percent")
                pct_s = f"{pct}%" if isinstance(pct, (int, float)) else "-"
                ams_s = format_ams_trays(rep)[:23]
                fname = rep.get("subtask_name") or rep.get("gcode_file") or "-"
                print(f"{name:<10} {model:<8} {state:<10} {noz:<12} {bed:<12} {pct_s:<7} {ams_s:<24} {fname}")
            except Exception as exc:
                print(f"{name:<10} {model:<8} {'ERR':<10} {'-':<12} {'-':<12} {'-':<7} {'-':<24} ({exc})")
    return 0


def cmd_bambu(args: argparse.Namespace) -> int:
    bambu_cfg = load_bambu_config()
    if args.bambu_cmd == "sync-codes":
        bs_codes = _bambu_studio_access_codes()
        print("Scanning Skynet for Bambu Lab printers (:8883)...")
        discovered = discover_bambu_printers()
        updated = 0
        for serial, ip in discovered.items():
            default_name, model = KNOWN_BAMBU_SERIALS.get(serial, (f"bambu-{serial[-4:].lower()}", _infer_bambu_model(serial)))
            # Keep existing friendly name if serial already present
            existing_name = next((k for k, v in bambu_cfg.items() if v.get("serial") == serial), default_name)
            entry = bambu_cfg.get(existing_name, {})
            entry["serial"] = serial
            entry["model"] = entry.get("model") or model
            entry["ip"] = ip
            if serial in bs_codes:
                entry["access_code"] = bs_codes[serial]
            bambu_cfg[existing_name] = entry
            has_code = "code=OK" if entry.get("access_code") else "code=MISSING"
            print(f"  {existing_name:<8} {entry['model']:<8} ip={ip:<15} serial={serial} ({has_code})")
            updated += 1
        save_bambu_config(bambu_cfg)
        print(f"Saved {updated} printer(s) to {BAMBU_CONFIG_PATH}")
        return 0

    if args.bambu_cmd == "discover":
        print("Scanning local subnet for Bambu Lab printers (:8883)...")
        discovered = discover_bambu_printers()
        for serial, ip in discovered.items():
            name = next((k for k, v in bambu_cfg.items() if v.get("serial") == serial), KNOWN_BAMBU_SERIALS.get(serial, ("?", ""))[0])
            print(f"  {name:<8} {_infer_bambu_model(serial):<8} ip={ip:<15} serial={serial}")
            if name in bambu_cfg:
                bambu_cfg[name]["ip"] = ip
        if bambu_cfg:
            save_bambu_config(bambu_cfg)
        return 0

    if args.bambu_cmd == "set":
        name = args.name.lower()
        entry = bambu_cfg.get(name, {})
        entry["serial"] = args.serial
        entry["access_code"] = args.code
        entry["model"] = args.model or entry.get("model") or _infer_bambu_model(args.serial)
        if args.ip:
            entry["ip"] = args.ip
        bambu_cfg[name] = entry
        save_bambu_config(bambu_cfg)
        print(f"Saved Bambu printer '{name}' ({entry['model']}, serial={args.serial}) to {BAMBU_CONFIG_PATH}")
        return 0

    return 1


def cmd_connect(args: argparse.Namespace) -> int:
    client = OctoPrintClient(args.printer)
    client.login()
    client.request("POST", "/api/connection", {"command": "connect"})
    print(f"Sent connect command to {client.host}.")
    return 0


def cmd_disconnect(args: argparse.Namespace) -> int:
    client = OctoPrintClient(args.printer)
    client.login()
    client.request("POST", "/api/connection", {"command": "disconnect"})
    print(f"Sent disconnect command to {client.host}.")
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    client = OctoPrintClient(args.printer)
    client.login()
    data = client.request("GET", "/api/files")
    files = data.get("files", [])
    if args.filter:
        needle = args.filter.lower()
        files = [f for f in files if needle in f.get("name", "").lower()]
    files.sort(key=lambda f: f.get("date") or 0, reverse=True)
    for f in files[: args.limit]:
        size_mb = (f.get("size") or 0) / (1024 * 1024)
        print(f"{size_mb:6.2f} MB  {f.get('name')}")
    print(f"\nShowing {min(len(files), args.limit)} of {len(files)} file(s) on {client.host}.")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    bambu_cfg = load_bambu_config()
    bambu_match = resolve_bambu_target(args.printer, bambu_cfg)
    if bambu_match:
        name, info = bambu_match
        ip = ensure_bambu_ip(name, info, bambu_cfg)
        upload_path = path
        md5_hex = ""
        tmp_dir_obj = None
        if path.suffix.lower() == ".gcode":
            tmp_dir_obj = tempfile.TemporaryDirectory()
            upload_path = Path(tmp_dir_obj.name) / f"{path.stem}.gcode.3mf"
            md5_hex = bambu_wrap_gcode_to_3mf(path, upload_path)
        try:
            bambu_ftps_upload(ip, info["access_code"], upload_path)
            print(f"Uploaded '{upload_path.name}' to Bambu {name} ({ip}) via FTPS.")
            if args.print_now:
                bambu_mqtt_start_project(ip, info["serial"], info["access_code"], upload_path.name, md5_hex=md5_hex)
                print(f"Sent project_file start for '{upload_path.name}' on Bambu {name} ({ip}).")
        finally:
            if tmp_dir_obj is not None:
                tmp_dir_obj.cleanup()
        return 0

    client = OctoPrintClient(args.printer)
    client.login()
    res = client.upload_gcode(path, select=args.select, start_print=args.print_now)
    name = res.get("files", {}).get("local", {}).get("name", path.name)
    action = "uploaded & started printing" if args.print_now else ("uploaded & selected" if args.select else "uploaded")
    print(f"Successfully {action} '{name}' on {client.host}.")
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    bambu_cfg = load_bambu_config()
    bambu_match = resolve_bambu_target(args.printer, bambu_cfg)
    if bambu_match:
        name, info = bambu_match
        ip = ensure_bambu_ip(name, info, bambu_cfg)
        if args.action == "start":
            if not args.file:
                print("Bambu 'job start' requires --file <name.gcode.3mf> (on the printer SD card).", file=sys.stderr)
                return 1
            bambu_mqtt_start_project(ip, info["serial"], info["access_code"], args.file)
            print(f"Sent project_file start for '{args.file}' on Bambu {name} ({ip}).")
            return 0
        cmd_map = {"pause": "pause", "resume": "resume", "cancel": "stop"}
        if args.action not in cmd_map:
            print("Bambu LAN MQTT supports: start (--file), pause, resume, cancel (stop).", file=sys.stderr)
            return 1
        bambu_mqtt_command(ip, info["serial"], info["access_code"], {"print": {"sequence_id": "0", "command": cmd_map[args.action], "param": ""}})
        print(f"Sent '{cmd_map[args.action]}' to Bambu {name} ({ip}). (Requires Developer Mode on locked-down firmware.)")
        return 0

    client = OctoPrintClient(args.printer)
    client.login()
    action = args.action
    if action == "pause":
        payload = {"command": "pause", "action": "pause"}
    elif action == "resume":
        payload = {"command": "pause", "action": "resume"}
    else:
        payload = {"command": action}
    client.request("POST", "/api/job", payload)
    print(f"Sent job '{action}' command to {client.host}.")
    return 0


def cmd_temp(args: argparse.Namespace) -> int:
    if args.tool is None and args.bed is None:
        print("Specify --tool <C> and/or --bed <C>.", file=sys.stderr)
        return 1
    client = OctoPrintClient(args.printer)
    client.login()
    if args.tool is not None:
        client.request("POST", "/api/printer/tool", {"command": "target", "targets": {"tool0": args.tool}})
        print(f"Set nozzle target on {client.host} to {args.tool}°C.")
    if args.bed is not None:
        client.request("POST", "/api/printer/bed", {"command": "target", "target": args.bed})
        print(f"Set bed target on {client.host} to {args.bed}°C.")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    bambu_cfg = load_bambu_config()
    bambu_match = resolve_bambu_target(args.printer, bambu_cfg)
    if bambu_match:
        name, info = bambu_match
        ip = ensure_bambu_ip(name, info, bambu_cfg)
        print(f"Watching Bambu {name} ({ip}) every {args.interval}s (Ctrl+C to stop)...")
        try:
            while True:
                rep = bambu_mqtt_query(ip, info["serial"], info["access_code"], timeout=5.0)
                ts = time.strftime("%H:%M:%S")
                print(
                    f"[{ts}] {rep.get('gcode_state', '?'):<10} | Nozzle: {rep.get('nozzle_temper', 0):5.1f}/{rep.get('nozzle_target_temper', 0):3.0f}C "
                    f"| Bed: {rep.get('bed_temper', 0):5.1f}/{rep.get('bed_target_temper', 0):3.0f}C "
                    f"| Progress: {rep.get('mc_percent', '-')}% | Left: {rep.get('mc_remaining_time', '-')}m"
                )
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped watching.")
            return 0

    client = OctoPrintClient(args.printer)
    client.login()
    print(f"Watching {client.host} every {args.interval}s (Ctrl+C to stop)...")
    try:
        while True:
            pr = client.request("GET", "/api/printer")
            job = client.request("GET", "/api/job")
            state = pr.get("state", {}).get("text", "?")
            t0 = pr.get("temperature", {}).get("tool0", {})
            bed = pr.get("temperature", {}).get("bed", {})
            prog = job.get("progress", {})
            pct = prog.get("completion")
            left = prog.get("printTimeLeft")
            pct_s = f"{pct:.1f}%" if isinstance(pct, (int, float)) else "-"
            left_s = f"{int(left // 60)}m{int(left % 60):02d}s" if isinstance(left, (int, float)) else "-"
            ts = time.strftime("%H:%M:%S")
            print(
                f"[{ts}] {state:<12} | Nozzle: {t0.get('actual', 0):5.1f}/{t0.get('target', 0):3.0f}C "
                f"| Bed: {bed.get('actual', 0):5.1f}/{bed.get('target', 0):3.0f}C "
                f"| Progress: {pct_s:<6} | Left: {left_s}"
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped watching.")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="erb335-printers", description="UTA ERB 335 Skynet + OctoPrint + Bambu Lab CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    # wifi
    p_wifi = sub.add_parser("wifi", help="Manage Skynet Wi-Fi connection")
    wsub = p_wifi.add_subparsers(dest="wifi_cmd", required=True)
    wsub.add_parser("status", help="Show current Wi-Fi status")
    p_wc = wsub.add_parser("connect", help="Scan + connect to Skynet")
    p_wc.add_argument("--ssid", default=DEFAULT_SSID)
    p_wc.add_argument("--timeout", type=int, default=20)
    wsub.add_parser("fix-profiles", help="Remove duplicate lowercase 'skynet' profile")

    # bambu
    p_bb = sub.add_parser("bambu", help="Manage Bambu Lab LAN discovery & local access-code cache")
    bsub = p_bb.add_subparsers(dest="bambu_cmd", required=True)
    bsub.add_parser("sync-codes", help="Discover Bambu printers & seed local cache from BambuStudio.conf")
    bsub.add_parser("discover", help="Scan local subnet for Bambu Lab printers (:8883)")
    p_bs = bsub.add_parser("set", help="Manually register/update a Bambu printer access code")
    p_bs.add_argument("name", help="Friendly name (e.g. h2d, p1s-1)")
    p_bs.add_argument("--serial", required=True)
    p_bs.add_argument("--code", required=True)
    p_bs.add_argument("--ip", default=None)
    p_bs.add_argument("--model", default=None)

    # status
    p_st = sub.add_parser("status", help="Show fleet or single printer status (OctoPrint + Bambu Lab)")
    p_st.add_argument("printer", nargs="?", help="Printer name (steve, saed, kate, chris, h2d, p1s-1, p1s-2, a1)")
    p_st.add_argument("--all", action="store_true", help="Include chris (under repair)")
    p_st.add_argument("--octoprint", action="store_true", help="Show OctoPrint printers only")
    p_st.add_argument("--bambu", action="store_true", help="Show Bambu Lab printers only")

    # connect / disconnect
    p_cn = sub.add_parser("connect", help="Connect OctoPrint serial link")
    p_cn.add_argument("printer")
    p_dc = sub.add_parser("disconnect", help="Disconnect OctoPrint serial link")
    p_dc.add_argument("printer")

    # files
    p_fl = sub.add_parser("files", help="List uploaded G-code files (OctoPrint)")
    p_fl.add_argument("printer")
    p_fl.add_argument("--limit", type=int, default=20)
    p_fl.add_argument("--filter", default=None)

    # upload
    p_up = sub.add_parser("upload", help="Upload .gcode (OctoPrint) or .3mf (Bambu FTPS)")
    p_up.add_argument("printer")
    p_up.add_argument("file")
    p_up.add_argument("--select", action="store_true", help="Select file after upload (OctoPrint)")
    p_up.add_argument("--print", dest="print_now", action="store_true", help="Start printing immediately (OctoPrint)")

    # job
    p_jb = sub.add_parser("job", help="Control current print job")
    p_jb.add_argument("printer")
    p_jb.add_argument("action", choices=["start", "pause", "resume", "cancel"])
    p_jb.add_argument("--file", default=None, help="Remote .gcode filename on SD card (for Bambu 'job start')")

    # temp
    p_tp = sub.add_parser("temp", help="Set nozzle / bed target temperatures (OctoPrint)")
    p_tp.add_argument("printer")
    p_tp.add_argument("--tool", type=float, default=None)
    p_tp.add_argument("--bed", type=float, default=None)

    # watch
    p_wt = sub.add_parser("watch", help="Live-monitor temperatures and print progress (OctoPrint or Bambu)")
    p_wt.add_argument("printer")
    p_wt.add_argument("--interval", type=float, default=5.0)

    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "wifi":
        if os.name != "nt":
            print("Wi-Fi helper commands currently support Windows only; connect to SSID 'Skynet' via your OS menu.", file=sys.stderr)
            return 1
        if args.wifi_cmd == "status":
            for k, v in get_wifi_status().items():
                print(f"{k.capitalize():<10}: {v}")
            return 0
        if args.wifi_cmd == "connect":
            return wifi_connect(args.ssid, args.timeout)
        if args.wifi_cmd == "fix-profiles":
            return wifi_fix_profiles()
    elif args.cmd == "bambu":
        return cmd_bambu(args)
    elif args.cmd == "status":
        return cmd_status(args)
    elif args.cmd == "connect":
        return cmd_connect(args)
    elif args.cmd == "disconnect":
        return cmd_disconnect(args)
    elif args.cmd == "files":
        return cmd_files(args)
    elif args.cmd == "upload":
        return cmd_upload(args)
    elif args.cmd == "job":
        return cmd_job(args)
    elif args.cmd == "temp":
        return cmd_temp(args)
    elif args.cmd == "watch":
        return cmd_watch(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

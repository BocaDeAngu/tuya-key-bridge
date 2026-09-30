#!/usr/bin/env python3
"""boca_tuya_bridge — NDJSON bridge between your app and Tuya's official SDK.

Extracts localKeys (plus DPs, IP, status) from the devices of a Smart Life /
Tuya account via QR login — no developer account, no Access ID/Secret.

INDEPENDENT REIMPLEMENTATION (MIT) on top of tuya-device-sharing-sdk (MIT,
Tuya). Contains no code from tuya-local-key (unlicensed); the QR-login idea
with Home Assistant's public app registration comes from the HA/LocalTuya
ecosystem.

Orchestrated use (your app spawns it and reads stdout):

    python -m boca_tuya_bridge run --user-code X --qr-png out.png [options]
    python -m boca_tuya_bridge print [--user-code X]                # human mode: QR opens in viewer, table
    python -m boca_tuya_bridge status --device-id X --ip Y --dp 1   # key via stdin
    python -m boca_tuya_bridge set --device-id X --ip Y --dp 1 --ligar true

`run` output: NDJSON — one JSON line per event, no stray text:

    {"event":"iniciando"}
    {"event":"sessao_invalida"}                       # cache existed, was invalid
    {"event":"devices","devices":[{...}]}             # valid cached session → straight to the list
    {"event":"qr","png":"out.png"}                    # QR generated — scan it in Smart Life
    {"event":"aguardando","restante":137}             # every poll (~2s) until confirmed
    {"event":"devices","devices":[{...}]}             # confirmed in the app
    {"event":"erro","mensagem":"..."}                 # any failure

Event names and keys above are the machine contract (kept stable); message
texts are English.

Local control (status/set): `set` also emits "status" (post-command read):

    {"event":"status","ligada":true,"dps":{"1":true,...}}  # dps = real state read

The localKey for status/set goes via STDIN (JSON {"key":"..."}) — never argv,
which is visible in the process list.

Exit codes: 0 ok · 2 usage · 3 QR expired · 1 error.

Security: the session (account tokens) is written atomically with mode 600;
localKeys go out ONLY in the devices event of stdout — nothing is logged or
saved in plain text beyond the SDK's own session cache.
"""

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

# Public device-sharing registration of the Home Assistant app (published in
# the HA core and in tuya-local — not a secret; the real authorization is the
# QR scan confirmed in YOUR app).
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"

DEFAULT_SESSION = os.path.expanduser("~/.config/boca-tuya-bridge/session.json")
DEFAULT_QR_PNG = os.path.join(os.getcwd(), "tuya-bridge-qr.png")
POLL_SECONDS = 2
DEFAULT_TIMEOUT = 150  # Tuya QRs expire fast (~1–2 min)

# Token fields we persist in the session (the SDK replaces them via update_token).
TOKEN_FIELDS = ("t", "uid", "expire_time", "access_token", "refresh_token")

REQUEST_TIMEOUT_SECONDS = 60

# Local control (tinytuya): tuyapi (Node, 2021) doesn't speak protocol 3.5 —
# newer plugs ignore its handshake; tinytuya 3.5 reads them (proven 2026-10).
DEFAULT_VERSION = "3.5"
DEFAULT_SOCKET_TIMEOUT = 5


def emit_event(objeto):
    """NDJSON: one JSON line per event, immediate flush (the reader is another process).
    ensure_ascii=True: 100% ASCII stdout — immune to Windows cp1252; the reader
    (Node/JSON.parse) decodes the unicode escapes back."""
    print(json.dumps(objeto, ensure_ascii=True), flush=True)


def exit_with(code):
    sys.stdout.flush()
    sys.exit(code)


# --------------------------------------------------------------------------- #
# Session (account tokens) — local cache, mode 600, atomic writes
# --------------------------------------------------------------------------- #
def load_session(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def atomic_write(path, data):
    """Atomic write (tmp + fsync + os.replace) with mode 600 — holds secrets."""
    path = os.fspath(path)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".bridge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class _SessionSaver:
    """Duck-typed for the SDK: called on every token auto-refresh."""

    def __init__(self, path, session):
        self.path = path
        self.session = session

    def update_token(self, token_info):
        self.session["token_info"] = {k: token_info.get(k) for k in TOKEN_FIELDS}
        atomic_write(self.path, json.dumps(self.session, indent=2).encode("utf-8"))


# --------------------------------------------------------------------------- #
# QR authentication (official SDK)
# --------------------------------------------------------------------------- #
def mint_qr_token(user_code):
    """Asks Tuya for the login QR token. Raises RuntimeError on failure."""
    from tuya_sharing import LoginControl

    resp = LoginControl().qr_code(CLIENT_ID, SCHEMA, user_code)
    if not resp.get("success"):
        raise RuntimeError(f"failed to start login [{resp.get('code')}]: {resp.get('msg')}")
    return resp["result"]["qrcode"]


def poll_login(token, user_code):
    """Single NON-blocking check: session if confirmed, None otherwise."""
    from tuya_sharing import LoginControl

    try:
        ok, result = LoginControl().login_result(token, CLIENT_ID, user_code)
    except Exception:
        return None
    if not ok:
        return None
    return {
        "client_id": CLIENT_ID,
        "user_code": user_code,
        "terminal_id": result.get("terminal_id"),
        "endpoint": result.get("endpoint") or result.get("end_point"),
        "token_info": {k: result.get(k) for k in TOKEN_FIELDS},
    }


def generate_qr_png(content, path):
    """QR PNG (qrcode[pil]) — the calling app serves this file to the user."""
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(content)
    qr.make(fit=True)
    qr.make_image().save(path)


def _set_sdk_timeout():
    """Defensive cap: if the SDK stops reading these globals, silent no-op."""
    try:
        import tuya_sharing.customerapi as customerapi
        import tuya_sharing.user as user

        customerapi.DEFAULT_TIMEOUT = user.DEFAULT_TIMEOUT = REQUEST_TIMEOUT_SECONDS
    except (ImportError, AttributeError):
        pass


def devices_from_session(session, session_path):
    """Device list (CustomerDevice) from a session, via Manager."""
    from tuya_sharing import Manager

    _set_sdk_timeout()
    manager = Manager(
        session.get("client_id", CLIENT_ID),
        session["user_code"],
        session["terminal_id"],
        session["endpoint"],
        session["token_info"],
        _SessionSaver(session_path, session),
    )
    manager.update_device_cache()  # renews the token if needed
    return list(manager.device_map.values())


# --------------------------------------------------------------------------- #
# Device serialization (JSON-safe)
# --------------------------------------------------------------------------- #
def _plain(value):
    """SimpleNamespace → dict; int keys (local_strategy) → str; recursive."""
    if isinstance(value, SimpleNamespace):
        value = vars(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def device_payload(device):
    """Fields the calling app uses: identity, connectivity, key and DPs."""
    fields = ("name", "id", "uuid", "local_key", "product_id", "product_name",
              "category", "model", "ip", "online", "sub", "support_local")
    out = {c: _plain(getattr(device, c, None)) for c in fields if hasattr(device, c)}
    out["online"] = bool(out.get("online"))
    for attr in ("status", "function", "status_range", "local_strategy"):
        value = getattr(device, attr, None)
        if value:
            out[attr] = _plain(value)
    return out


# --------------------------------------------------------------------------- #
# Local control (tinytuya, TCP 6668) — status and on/off
# --------------------------------------------------------------------------- #
def _local_device(device_id, ip, key, version, timeout):
    import tinytuya

    if not ip:
        raise RuntimeError("device IP on the LAN is required for local control")
    return tinytuya.OutletDevice(device_id, ip, key, version=version, connection_timeout=timeout)


def _read_dps(d):
    """Real state (all DPs, metering included); keys as str. Raises on device
    error (wrong key, unreachable, protocol mismatch) instead of returning an
    empty dict — silent emptiness would look like a valid OFFLINE device."""
    st = d.status() or {}
    if st.get("Error"):
        raise RuntimeError(f"device did not answer: {st['Error']}")
    return {str(k): v for k, v in (st.get("dps") or {}).items()}


def local_status(device_id, ip, key, dp, version=DEFAULT_VERSION, timeout=DEFAULT_SOCKET_TIMEOUT):
    d = _local_device(device_id, ip, key, version, timeout)
    dps = _read_dps(d)
    return {"ligada": dps.get(str(dp)) is True, "dps": dps}


def local_command(device_id, ip, key, dp, on, version=DEFAULT_VERSION, timeout=DEFAULT_SOCKET_TIMEOUT):
    d = _local_device(device_id, ip, key, version, timeout)
    d.set_status(bool(on), dp)
    dps = _read_dps(d)  # re-read: act only after reading the real state
    return {"ligada": dps.get(str(dp)) is True, "dps": dps}


def _key_from_stdin(args):
    """localKey: --key exists for manual testing, but the normal path is stdin
    (argv stays in the process list/history; stdin doesn't). stdin receives
    JSON {"key":"..."}."""
    if args.key:
        return args.key
    line = sys.stdin.readline().strip()
    if not line:
        raise RuntimeError("localKey not provided (stdin JSON {\"key\":\"...\"} or --key)")
    try:
        data = json.loads(line)
        key = data.get("key") if isinstance(data, dict) else None
    except json.JSONDecodeError:
        key = None
    if not key or not isinstance(key, str):
        raise RuntimeError('stdin must be JSON {"key":"..."}')
    return key


# --------------------------------------------------------------------------- #
# Main flow (shared between run and print)
# --------------------------------------------------------------------------- #
def _collect_devices(args, on_event):
    """Cached session → QR → poll → devices. `on_event(dict)` receives the same
    events of the NDJSON contract; returns (code, devices|None)."""
    on_event({"event": "iniciando"})

    # 1. Valid cached session → devices straight away, no QR (2nd run onwards).
    if not args.relogin:
        session = load_session(args.session)
        if session:
            try:
                return 0, devices_from_session(session, args.session)
            except Exception as e:
                on_event({"event": "sessao_invalida", "motivo": str(e)})
                # falls through to the QR

    # 2. No user code and no session → usage error (the calling app validates
    #    first, but the bridge defends itself: NEVER interactive input()).
    if not args.user_code:
        on_event({"event": "erro", "mensagem": "user code required (Smart Life > Me > Account and Security > User Code)"})
        return 2, None

    # 3. Generates the login QR and saves the PNG.
    try:
        token = mint_qr_token(args.user_code)
    except Exception as e:
        on_event({"event": "erro", "mensagem": f"failed to generate QR: {e} — check the user code"})
        return 1, None
    try:
        generate_qr_png(f"{args.qr_scheme}--qrLogin?token={token}", args.qr_png)
    except Exception as e:
        on_event({"event": "erro", "mensagem": f"failed to generate the QR PNG: {e}"})
        return 1, None
    on_event({"event": "qr", "png": os.path.abspath(args.qr_png)})

    # 4. Polls until the app confirms (QRs expire fast — deadline in --timeout).
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        session = poll_login(token, args.user_code)
        if session:
            break
        on_event({"event": "aguardando", "restante": int(deadline - time.time())})
        time.sleep(POLL_SECONDS)
    if not session:
        on_event({"event": "erro", "mensagem": f"login expired ({args.timeout}s) — Tuya QRs expire fast; reimport with a new QR"})
        return 3, None

    # 5. Persists the session (600) and fetches the devices.
    atomic_write(args.session, json.dumps(session, indent=2).encode("utf-8"))
    try:
        return 0, devices_from_session(session, args.session)
    except Exception as e:
        on_event({"event": "erro", "mensagem": f"logged in, but failed to list devices: {e}"})
        return 1, None


def run(args):
    """Machine-to-machine mode: NDJSON contract on stdout (consumed by apps)."""
    code, devices = _collect_devices(args, emit_event)
    if code == 0 and devices is not None:
        emit_event({"event": "devices", "devices": [device_payload(d) for d in devices]})
    return code


def _open_in_viewer(path):
    """Opens the PNG in the OS default viewer (human mode only)."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # Windows-only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def _is_private_ip(ip):
    """Simplified RFC1918 — Tuya's cloud sometimes reports the router WAN IP."""
    if ip.startswith(("192.168.", "10.")):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def _device_table(devices):
    """Aligned rows: name · id · ip · online · key (what the user came for)."""
    rows = []
    for d in devices:
        p = device_payload(d)
        rows.append({
            "name": str(p.get("name") or "(unnamed)"),
            "id": str(p.get("id") or "?"),
            "ip": str(p.get("ip") or "-"),
            "online": "online" if p.get("online") else "OFFLINE",
            "key": str(p.get("local_key") or "(no key)"),
        })
    widths = {c: max(len(r[c]) for r in rows) for c in ("name", "id", "ip", "online")}
    return [
        "  {0}  {1}  ip {2}  {3}  key {4}".format(
            r["name"].ljust(widths["name"]), r["id"].ljust(widths["id"]),
            r["ip"].ljust(widths["ip"]), r["online"].ljust(widths["online"]), r["key"])
        for r in rows
    ]


def print_human(args):
    """Human mode: same flow as run, readable text — QR opens in the OS default
    viewer, devices in a table. NOT NDJSON. Text in EN/ASCII: immune to Windows
    cp1252 (same principle as ensure_ascii in machine mode)."""
    pending = {"line": False}

    def on_event(e):
        t = e.get("event")
        if t == "sessao_invalida":
            print("Cached session expired - new QR login needed.")
        elif t == "qr":
            opened = _open_in_viewer(e["png"])
            where = "that just opened" if opened else f"at {e['png']}"
            print(f"\nScan the QR {where} in the Smart Life app: Me > Scan > Confirm login")
        elif t == "aguardando":
            pending["line"] = True
            print(f"\r  waiting for confirmation in the app... {e['restante']}s left   ", end="", flush=True)
        elif t == "erro":
            if pending["line"]:
                print()
                pending["line"] = False
            msg = e["mensagem"].encode("ascii", "replace").decode("ascii")
            print(f"ERROR: {msg}", file=sys.stderr)

    code, devices = _collect_devices(args, on_event)
    if pending["line"]:
        print()
    if code != 0:
        return code
    if not devices:
        print("No devices in your account.")
        return 0
    print(f"{len(devices)} device(s) in your account:")
    for row in _device_table(devices):
        print(row)
    ips = [str(device_payload(d).get("ip") or "") for d in devices]
    if any(ips) and not all(_is_private_ip(i) for i in ips):
        print("\nNOTE: an IP above is public/WAN (the cloud reports it). For local control, find the")
        print("device's LAN IP (Smart Life app > device > Edit, or your router's DHCP) and pass --ip.")
    print("\nLocal control (device on the same Wi-Fi; key via stdin, not argv):")
    print('  echo \'{"key":"..."}\' | boca-tuya-bridge status --device-id <id> --ip <ip>')
    print('  echo \'{"key":"..."}\' | boca-tuya-bridge set --device-id <id> --ip <ip> --ligar true')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="boca_tuya_bridge", description="NDJSON bridge of Smart Life/Tuya localKeys (QR login, no dev account)")
    p.add_argument("command", choices=["run", "print", "status", "set"], help="run=NDJSON (machine) · print=human (QR opens, table) · status=read · set=on/off")
    p.add_argument("--user-code", help="Smart Life User Code (Me > Account and Security)")
    p.add_argument("--device-id", help="[status/set] device id")
    p.add_argument("--ip", help="[status/set] device IP on the LAN")
    p.add_argument("--key", help="[status/set] localKey (prefer stdin — argv stays in history/process list)")
    p.add_argument("--dp", type=int, default=1, help="[status/set] relay DP (default 1)")
    p.add_argument("--ligar", choices=("true", "false"), help="[set] true=on, false=off (flag kept for the machine contract)")
    p.add_argument("--versao", choices=("3.1", "3.3", "3.4", "3.5"), default=DEFAULT_VERSION, help="[status/set] device protocol")
    p.add_argument("--timeout", type=int, default=None, help="[run/print] seconds waiting for the scan (150) · [status/set] socket timeout in s (5)")
    p.add_argument("--qr-png", default=DEFAULT_QR_PNG, help="where to save the QR PNG")
    p.add_argument("--session", default=DEFAULT_SESSION, help="session cache path (600)")
    p.add_argument("--qr-scheme", choices=["smartlife", "tuyaSmart"], default="smartlife", help="QR prefix (Smart Life reads both)")
    p.add_argument("--relogin", action="store_true", help="ignore the cached session and ask for a new QR")
    args = p.parse_args(argv)

    if args.command in ("status", "set"):
        try:
            if not args.device_id:
                emit_event({"event": "erro", "mensagem": "--device-id is required"})
                return 2
            key = _key_from_stdin(args)
            timeout = DEFAULT_SOCKET_TIMEOUT if args.timeout is None else args.timeout
            if args.command == "status":
                emit_event({"event": "status", **local_status(args.device_id, args.ip, key, args.dp, args.versao, timeout)})
            else:
                if args.ligar is None:
                    emit_event({"event": "erro", "mensagem": "--ligar true|false is required"})
                    return 2
                emit_event({"event": "status", **local_command(args.device_id, args.ip, key, args.dp, args.ligar == "true", args.versao, timeout)})
            return 0
        except Exception as e:
            emit_event({"event": "erro", "mensagem": str(e)})
            return 1

    args.timeout = DEFAULT_TIMEOUT if args.timeout is None else args.timeout  # run/print: QR timeout
    if args.command == "print":
        try:
            return print_human(args)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
    try:
        return run(args)
    except KeyboardInterrupt:
        emit_event({"event": "erro", "mensagem": "interrupted"})
        return 130
    except Exception as e:  # network down, SDK changed, etc — never a raw traceback to the reader
        emit_event({"event": "erro", "mensagem": str(e)})
        return 1


if __name__ == "__main__":
    exit_with(main())

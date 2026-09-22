#!/usr/bin/env python3
"""loq-kbd - fixed colors and lighting effects for the Lenovo LOQ RGB keyboard.

Runs on Linux and Windows with nothing but the standard library: the keyboard
speaks HID LampArray (HID Usage Page 0x59), so the wire format is identical on
both systems and only the transport differs.

    Linux    ioctl(HIDIOCSFEATURE) on /dev/hidrawN
    Windows  HidD_SetFeature from hid.dll, reached through ctypes

Why this exists: the keyboard's controller boots into "autonomous mode" and runs
its own colour-cycling effect internally, with no involvement from the operating
system. That is why the colour keeps changing on its own. Clearing one bit takes
the effect away and hands control to the host.
"""

from __future__ import annotations

import colorsys
import math
import os
import signal
import struct
import sys
import time

VENDOR_ID = 0x048D          # ITE Tech. Inc.
PRODUCT_ID = 0xC693         # ITE Device(8258) - the LOQ lighting controller
LIGHTING_USAGE_PAGE = 0x59  # HID "Lighting And Illumination"

# HID LampArray report ids, and each report's total size in bytes including the
# leading report id. The sizes come from the device's own report descriptor, not
# from guesswork:  xxd /sys/class/hidraw/hidrawN/device/report_descriptor
R_ATTRIBUTES, SZ_ATTRIBUTES = 1, 23     # GET  LampArrayAttributes
R_ATTR_REQUEST, SZ_ATTR_REQUEST = 2, 3  # SET  LampAttributesRequest
R_ATTR_RESPONSE, SZ_ATTR_RESPONSE = 3, 29   # GET  LampAttributesResponse
R_MULTI_UPDATE, SZ_MULTI_UPDATE = 4, 51     # SET  LampMultiUpdate (<=8 lamps)
R_RANGE_UPDATE, SZ_RANGE_UPDATE = 5, 10     # SET  LampRangeUpdate
R_CONTROL, SZ_CONTROL = 6, 2                # SET  LampArrayControl

FLAG_UPDATE_COMPLETE = 0x01  # LampUpdateFlags: apply this batch now
LAMPS_PER_MULTI_UPDATE = 8   # report 4 carries at most 8 lamps

LAMP_ARRAY_KINDS = {
    1: "keyboard", 2: "mouse", 3: "game controller", 4: "peripheral",
    5: "scene", 6: "notification", 7: "chassis", 8: "wearable",
    9: "furniture", 10: "art",
}
LAMP_PURPOSES = {1: "control", 2: "accent", 4: "branding", 8: "status"}


# --------------------------------------------------------------------------
# Colour names, in English and Spanish. Both spellings are always accepted.
# --------------------------------------------------------------------------

COLOR_NAMES: dict[str, tuple[str, str]] = {
    # hex:       (english,    spanish)
    "ff0000": ("red", "rojo"),
    "00ff00": ("green", "verde"),
    "0000ff": ("blue", "azul"),
    "ffffff": ("white", "blanco"),
    "000000": ("black", "negro"),
    "00ffff": ("cyan", "cian"),
    "ff00ff": ("magenta", "magenta"),
    "ffff00": ("yellow", "amarillo"),
    "ff5500": ("orange", "naranja"),
    "8800ff": ("purple", "morado"),
    "ff1493": ("pink", "rosa"),
    "00ff88": ("mint", "menta"),
    "ffd700": ("gold", "dorado"),
    "c0c0c0": ("silver", "plata"),
    "008080": ("teal", "verdeazulado"),
    "4b0082": ("indigo", "indigo"),
    "a52a2a": ("brown", "marron"),
    "808080": ("gray", "gris"),
    "7fff00": ("lime", "lima"),
    "40e0d0": ("turquoise", "turquesa"),
    "ff7f50": ("coral", "coral"),
    "e6e6fa": ("lavender", "lavanda"),
}

# Flattened lookup plus a few extra aliases people reach for.
COLORS: dict[str, str] = {}
for _hex, _names in COLOR_NAMES.items():
    for _n in _names:
        COLORS[_n] = _hex
COLORS.update({
    "gray": "808080", "grey": "808080", "violet": "8800ff",
    "violeta": "8800ff", "off": "000000", "apagado": "000000",
    "warm": "ff8c42", "calido": "ff8c42", "cold": "42a5ff", "frio": "42a5ff",
})


def parse_color(text: str) -> tuple[int, int, int]:
    """Accept a name in either language, or hex as RGB/RRGGBB, with or without #."""
    key = text.strip().lower().lstrip("#")
    key = COLORS.get(key, key)
    if len(key) == 3:                     # f80 -> ff8800
        key = "".join(c * 2 for c in key)
    if len(key) != 6:
        raise ValueError(f"not a colour name or hex value: {text!r}")
    try:
        value = int(key, 16)
    except ValueError:
        raise ValueError(f"not a colour name or hex value: {text!r}") from None
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def scale(rgb: tuple[int, int, int], percent: float) -> tuple[int, int, int]:
    """Apply brightness by scaling the channels.

    Every mode does it this way rather than using the Intensity field of report
    5. Scaling is correct on any LampArray, while Intensity is optional and this
    controller reports IntensityLevelCount = 1, meaning it ignores it outright.
    """
    factor = max(0.0, min(100.0, percent)) / 100.0
    return tuple(round(c * factor) for c in rgb)  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Transport backends. Each one only has to move feature reports.
# --------------------------------------------------------------------------

class DeviceNotFound(Exception):
    pass


class AccessDenied(Exception):
    pass


class Backend:
    """A way to exchange HID feature reports with the lighting interface."""

    description = "generic"

    def set_feature(self, data: bytes) -> None:
        raise NotImplementedError

    def get_feature(self, report_id: int, size: int) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class LinuxBackend(Backend):
    """hidraw. Feature reports go through two ioctls."""

    def __init__(self) -> None:
        import fcntl

        self._fcntl = fcntl
        self.path = self._locate()
        self.description = self.path
        try:
            self.fd = os.open(self.path, os.O_RDWR)
        except PermissionError as exc:
            raise AccessDenied(
                f"no permission on {self.path}. Run 'sudo ./install.sh' once to "
                f"install the udev rule, or use sudo."
            ) from exc

    @staticmethod
    def _ioctl_code(nr: int, size: int) -> int:
        # _IOC(_IOC_WRITE|_IOC_READ, 'H', nr, size)
        return (3 << 30) | (size << 16) | (ord("H") << 8) | nr

    @staticmethod
    def _locate() -> str:
        """Find the hidraw node for the lighting interface.

        The hidraw number moves around depending on USB enumeration order, so it
        is never hardcoded. Both of the keyboard's interfaces share the same
        VID:PID, so the lighting one is picked out by its report descriptor
        starting with the Lighting usage page (05 59).
        """
        import glob

        want = f"0003:{VENDOR_ID:08X}:{PRODUCT_ID:08X}"
        for sysdir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
            try:
                with open(os.path.join(sysdir, "device", "uevent")) as fh:
                    fields = dict(
                        line.strip().split("=", 1) for line in fh if "=" in line
                    )
                if fields.get("HID_ID", "").upper() != want:
                    continue
                desc = os.path.join(sysdir, "device", "report_descriptor")
                with open(desc, "rb") as fh:
                    if fh.read(2) != bytes([0x05, LIGHTING_USAGE_PAGE]):
                        continue
            except OSError:
                continue
            return "/dev/" + os.path.basename(sysdir)
        raise DeviceNotFound(
            f"no LampArray interface for {VENDOR_ID:04x}:{PRODUCT_ID:04x}. "
            f"Is this a Lenovo LOQ with an RGB keyboard?"
        )

    def set_feature(self, data: bytes) -> None:
        buf = bytearray(data)
        self._fcntl.ioctl(self.fd, self._ioctl_code(0x06, len(buf)), buf, True)

    def get_feature(self, report_id: int, size: int) -> bytes:
        buf = bytearray(size)
        buf[0] = report_id
        self._fcntl.ioctl(self.fd, self._ioctl_code(0x07, size), buf, True)
        return bytes(buf)

    def close(self) -> None:
        os.close(self.fd)


class WindowsBackend(Backend):
    """hid.dll through ctypes, with SetupAPI to enumerate the interfaces.

    Two behaviours differ from Linux and both matter:

    * HidD_SetFeature and HidD_GetFeature demand a buffer exactly
      FeatureReportByteLength long, the largest feature report the collection
      declares. Short buffers are rejected, so every report is zero padded to
      that length. On Linux the buffer is the report's own size.
    * Every function below has an explicit restype. Without one ctypes assumes
      a 32 bit int, which silently truncates the 64 bit HANDLE from CreateFileW
      and the HDEVINFO from SetupDiGetClassDevsW. The HidD_* calls return
      BOOLEAN, one byte, not the four byte BOOL.
    """

    HIDP_STATUS_SUCCESS = 0x00110000

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        hid = ctypes.WinDLL("hid", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._hid, self._kernel32 = hid, kernel32

        HDEVINFO = ctypes.c_void_p
        PREPARSED = ctypes.c_void_p
        BOOLEAN = ctypes.c_ubyte      # what HidD_* actually return
        NTSTATUS = ctypes.c_long

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
            ]

        class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 512),
            ]

        class HIDD_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT),
                ("ProductID", wintypes.USHORT),
                ("VersionNumber", wintypes.USHORT),
            ]

        class HIDP_CAPS(ctypes.Structure):
            _fields_ = [
                ("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
                ("InputReportByteLength", wintypes.USHORT),
                ("OutputReportByteLength", wintypes.USHORT),
                ("FeatureReportByteLength", wintypes.USHORT),
                ("Reserved", wintypes.USHORT * 17),
                ("NumberLinkCollectionNodes", wintypes.USHORT),
                ("NumberInputButtonCaps", wintypes.USHORT),
                ("NumberInputValueCaps", wintypes.USHORT),
                ("NumberInputDataIndices", wintypes.USHORT),
                ("NumberOutputButtonCaps", wintypes.USHORT),
                ("NumberOutputValueCaps", wintypes.USHORT),
                ("NumberOutputDataIndices", wintypes.USHORT),
                ("NumberFeatureButtonCaps", wintypes.USHORT),
                ("NumberFeatureValueCaps", wintypes.USHORT),
                ("NumberFeatureDataIndices", wintypes.USHORT),
            ]

        setupapi.SetupDiGetClassDevsW.restype = HDEVINFO
        setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
        ]
        setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            HDEVINFO, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
            ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ]
        setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
        setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            HDEVINFO, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
            ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W),
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [HDEVINFO]

        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        hid.HidD_GetAttributes.restype = BOOLEAN
        hid.HidD_GetAttributes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES),
        ]
        hid.HidD_GetPreparsedData.restype = BOOLEAN
        hid.HidD_GetPreparsedData.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PREPARSED),
        ]
        hid.HidD_FreePreparsedData.restype = BOOLEAN
        hid.HidD_FreePreparsedData.argtypes = [PREPARSED]
        hid.HidP_GetCaps.restype = NTSTATUS
        hid.HidP_GetCaps.argtypes = [PREPARSED, ctypes.POINTER(HIDP_CAPS)]
        hid.HidD_SetFeature.restype = BOOLEAN
        hid.HidD_SetFeature.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG,
        ]
        hid.HidD_GetFeature.restype = BOOLEAN
        hid.HidD_GetFeature.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG,
        ]

        # GUID_DEVINTERFACE_HID {4D1E55B2-F16F-11CF-88CB-001111000030}
        hid_guid = GUID(
            0x4D1E55B2, 0xF16F, 0x11CF,
            (ctypes.c_ubyte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30),
        )

        DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x02, 0x10
        GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
        FILE_SHARE_READ, FILE_SHARE_WRITE = 0x01, 0x02
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        collection = setupapi.SetupDiGetClassDevsW(
            ctypes.byref(hid_guid), None, None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
        )
        if not collection or collection == INVALID_HANDLE_VALUE:
            raise DeviceNotFound(
                f"SetupDiGetClassDevsW failed: "
                f"{ctypes.WinError(ctypes.get_last_error())}"
            )

        self.handle = None
        self.feature_len = 0

        try:
            index = 0
            while True:
                iface = SP_DEVICE_INTERFACE_DATA()
                iface.cbSize = ctypes.sizeof(iface)
                if not setupapi.SetupDiEnumDeviceInterfaces(
                    collection, None, ctypes.byref(hid_guid), index,
                    ctypes.byref(iface),
                ):
                    break
                index += 1

                detail = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
                # cbSize is the size of the fixed header as Windows declares it
                # (DWORD + WCHAR[1]), not the size of this oversized buffer.
                detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    collection, ctypes.byref(iface), ctypes.byref(detail),
                    ctypes.sizeof(detail), None, None,
                ):
                    continue

                handle = kernel32.CreateFileW(
                    detail.DevicePath, GENERIC_READ | GENERIC_WRITE,
                    FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                    0, None,
                )
                if not handle or handle == INVALID_HANDLE_VALUE:
                    # Windows reserves keyboard and mouse collections for
                    # itself, so refusals here are expected. Skip quietly.
                    continue

                keep = False
                try:
                    attrs = HIDD_ATTRIBUTES()
                    attrs.Size = ctypes.sizeof(attrs)
                    if not hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
                        continue
                    if (attrs.VendorID, attrs.ProductID) != (VENDOR_ID, PRODUCT_ID):
                        continue

                    preparsed = PREPARSED()
                    if not hid.HidD_GetPreparsedData(
                        handle, ctypes.byref(preparsed)
                    ):
                        continue
                    try:
                        caps = HIDP_CAPS()
                        if hid.HidP_GetCaps(
                            preparsed, ctypes.byref(caps)
                        ) != self.HIDP_STATUS_SUCCESS:
                            continue
                        if caps.UsagePage != LIGHTING_USAGE_PAGE:
                            continue
                        self.feature_len = caps.FeatureReportByteLength
                    finally:
                        hid.HidD_FreePreparsedData(preparsed)

                    self.handle = handle
                    self.description = detail.DevicePath
                    keep = True
                    break
                finally:
                    if not keep:
                        kernel32.CloseHandle(handle)
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(collection)

        if self.handle is None:
            raise DeviceNotFound(
                f"no LampArray interface for {VENDOR_ID:04x}:{PRODUCT_ID:04x}. "
                f"If Windows Dynamic Lighting is on, turn it off in Settings > "
                f"Personalization > Dynamic Lighting: it holds the device."
            )
        if self.feature_len < SZ_MULTI_UPDATE:
            self.feature_len = SZ_MULTI_UPDATE

    def set_feature(self, data: bytes) -> None:
        buf = self._ctypes.create_string_buffer(
            bytes(data).ljust(self.feature_len, b"\x00"), self.feature_len
        )
        if not self._hid.HidD_SetFeature(self.handle, buf, self.feature_len):
            raise OSError(
                f"HidD_SetFeature failed for report {data[0]}: "
                f"{self._ctypes.WinError(self._ctypes.get_last_error())}"
            )

    def get_feature(self, report_id: int, size: int) -> bytes:
        buf = self._ctypes.create_string_buffer(self.feature_len)
        buf[0] = bytes([report_id])
        if not self._hid.HidD_GetFeature(self.handle, buf, self.feature_len):
            raise OSError(
                f"HidD_GetFeature failed for report {report_id}: "
                f"{self._ctypes.WinError(self._ctypes.get_last_error())}"
            )
        return bytes(buf.raw[:size])

    def close(self) -> None:
        if self.handle is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def open_backend() -> Backend:
    if sys.platform.startswith("win"):
        return WindowsBackend()
    if sys.platform.startswith("linux"):
        return LinuxBackend()
    raise DeviceNotFound(f"unsupported platform: {sys.platform}")


# --------------------------------------------------------------------------
# The LampArray itself
# --------------------------------------------------------------------------

class LampArray:
    def __init__(self) -> None:
        self.backend = open_backend()
        self._attributes: dict | None = None

    # -- plumbing ---------------------------------------------------------

    def reopen(self) -> bool:
        """Reconnect after the device went away, e.g. across a suspend."""
        try:
            self.backend.close()
        except Exception:
            pass
        for _ in range(20):
            try:
                self.backend = open_backend()
                return True
            except (DeviceNotFound, AccessDenied, OSError):
                time.sleep(0.5)
        return False

    def close(self) -> None:
        self.backend.close()

    # -- reads ------------------------------------------------------------

    @property
    def attributes(self) -> dict:
        if self._attributes is None:
            raw = self.backend.get_feature(R_ATTRIBUTES, SZ_ATTRIBUTES)
            count, width, height, depth, kind, interval = struct.unpack(
                "<HIIIII", raw[1:SZ_ATTRIBUTES]
            )
            self._attributes = {
                "lamps": count, "width_um": width, "height_um": height,
                "depth_um": depth, "kind": kind, "min_interval_us": interval,
            }
        return self._attributes

    @property
    def lamp_count(self) -> int:
        return self.attributes["lamps"]

    def lamp(self, lamp_id: int) -> dict:
        """One zone's attributes. The standard makes this a two step exchange:
        ask with report 2, collect the answer from report 3."""
        self.backend.set_feature(struct.pack("<BH", R_ATTR_REQUEST, lamp_id))
        raw = self.backend.get_feature(R_ATTR_RESPONSE, SZ_ATTR_RESPONSE)
        # The five u32 fields follow DECLARATION order in the descriptor, which
        # is not usage order: X, Y, Z, UpdateLatency, LampPurposes. Reading them
        # in usage order swaps the last two.
        lid, x, y, z, latency, purposes = struct.unpack("<HIIIII", raw[1:23])
        red, green, blue, intensity, programmable, binding = raw[23:29]
        return {
            "id": lid, "x_um": x, "y_um": y, "z_um": z,
            "latency_us": latency, "purposes": purposes,
            "red_levels": red, "green_levels": green, "blue_levels": blue,
            "intensity_levels": intensity,
            "programmable": bool(programmable), "key": binding,
        }

    @property
    def intensity_is_real(self) -> bool:
        """Whether the Intensity field of report 5 does anything at all.

        The ITE 8258 reports IntensityLevelCount = 1, meaning it does not: the
        standard allows ignoring intensity on a full RGB lamp. Brightness then
        has to be emulated by scaling R, G and B. This is asked rather than
        assumed, so the code stays correct on a LampArray that honours it.
        """
        return self.lamp(0)["intensity_levels"] > 1

    # -- writes -----------------------------------------------------------

    def set_autonomous(self, enabled: bool) -> None:
        """Hand lighting control to the firmware, or take it away.

        Clearing this is what stops the built in colour cycling. It has to
        happen BEFORE any painting: while the firmware still owns the lamps it
        repaints over anything the host writes, within milliseconds.
        """
        self.backend.set_feature(bytes([R_CONTROL, 1 if enabled else 0]))

    def fill(self, rgb: tuple[int, int, int], intensity: int = 0xFF) -> None:
        """Paint every zone one colour with a single report 5."""
        r, g, b = rgb
        self.backend.set_feature(struct.pack(
            "<BBHHBBBB", R_RANGE_UPDATE, FLAG_UPDATE_COMPLETE,
            0, max(0, self.lamp_count - 1), r, g, b, intensity,
        ))

    def paint(self, colors: list[tuple[int, int, int]]) -> None:
        """Paint zone i with colors[i], using report 4 in batches of eight.

        UpdateComplete is set only on the final batch, so the whole frame lands
        at once instead of tearing across the keyboard.
        """
        total = min(len(colors), self.lamp_count)
        for start in range(0, total, LAMPS_PER_MULTI_UPDATE):
            ids = list(range(start, min(start + LAMPS_PER_MULTI_UPDATE, total)))
            last = start + LAMPS_PER_MULTI_UPDATE >= total
            packet = struct.pack(
                "<BBB", R_MULTI_UPDATE, len(ids),
                FLAG_UPDATE_COMPLETE if last else 0,
            )
            packet += b"".join(struct.pack("<H", i) for i in ids)
            packet += b"".join(bytes([*colors[i], 0xFF]) for i in ids)
            self.backend.set_feature(packet.ljust(SZ_MULTI_UPDATE, b"\x00"))


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

FPS = 30


def _hsv(hue: float, value: float = 1.0) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, 1.0, value)
    return round(r * 255), round(g * 255), round(b * 255)


def mode_static(dev: LampArray, rgb, brightness, animate):
    dev.fill(scale(rgb, brightness))
    return False    # nothing to animate


def mode_gradient(dev: LampArray, rgb, brightness, animate):
    """A still rainbow spread left to right across the zones."""
    n = dev.lamp_count
    dev.paint([_hsv(i / n, brightness / 100) for i in range(n)])
    return False


def mode_breathing(dev: LampArray, rgb, brightness, animate):
    """One colour fading in and out. Brightness sets the peak."""
    def frame(t: float) -> None:
        # A raised cosine: it lingers at the dark end, which reads as breathing
        # rather than as a triangle wave.
        phase = (1 - math.cos(t * 1.6)) / 2
        dev.fill(scale(rgb, brightness * phase))
    return frame


def mode_wave(dev: LampArray, rgb, brightness, animate):
    """A rainbow travelling along the keyboard."""
    n = dev.lamp_count

    def frame(t: float) -> None:
        offset = t * 0.25
        dev.paint([_hsv(i / n + offset, brightness / 100) for i in range(n)])
    return frame


def mode_spectrum(dev: LampArray, rgb, brightness, animate):
    """Every zone the same colour, cycling through the spectrum together."""
    def frame(t: float) -> None:
        dev.fill(_hsv(t * 0.08, brightness / 100))
    return frame


# number -> (english name, spanish name, function, animated?, description)
MODES: dict[int, tuple[str, str, object, bool, str]] = {
    1: ("static", "estatico", mode_static, False,
        "one fixed colour on every zone"),
    2: ("breathing", "respiracion", mode_breathing, True,
        "one colour fading in and out"),
    3: ("wave", "ola", mode_wave, True,
        "a rainbow travelling along the keyboard"),
    4: ("gradient", "degradado", mode_gradient, False,
        "a still rainbow spread left to right"),
    5: ("spectrum", "espectro", mode_spectrum, True,
        "all zones cycling through the spectrum together"),
}

MODE_LOOKUP: dict[str, int] = {}
for _num, (_en, _es, *_rest) in MODES.items():
    for _alias in (_en, _es, f"mode{_num}", f"modo{_num}", f"m{_num}", str(_num)):
        MODE_LOOKUP[_alias] = _num


def resolve_mode(text: str) -> int | None:
    key = text.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    return MODE_LOOKUP.get(key)


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def run_mode(dev: LampArray, number: int, rgb, brightness: float) -> int:
    name_en, name_es, func, animated, _desc = MODES[number]

    # Take the lamps away from the firmware first, or it repaints over us.
    dev.set_autonomous(False)

    # Echo what actually reached the keyboard. For a single colour that means
    # the post-brightness value, which is the only way to see that scaling ran.
    if number == 1:
        r, g, b = scale(rgb, brightness)
        detail = f"at #{r:02x}{g:02x}{b:02x}"
        if brightness < 100:
            detail += f" (brightness {brightness:g}%)"
    elif number == 2:
        r, g, b = rgb
        detail = f"on #{r:02x}{g:02x}{b:02x}, peaking at {brightness:g}%"
    else:
        detail = f"at brightness {brightness:g}%"

    frame = func(dev, rgb, brightness, animated)
    label = f"mode {number} ({name_en} / {name_es})"
    if not frame:
        print(f"{label}: {dev.lamp_count} zones {detail}")
        return 0

    # flush matters: stdout is block buffered when this runs under systemd or
    # any pipe, and the process is killed by a signal, so an unflushed line
    # would never reach the journal.
    print(f"{label} running: {dev.lamp_count} zones {detail}. Ctrl-C to stop.",
          flush=True)

    # Let systemctl stop and restart end the loop the same way Ctrl-C does,
    # instead of killing the process mid frame.
    try:
        signal.signal(signal.SIGTERM, _raise_interrupt)
    except (ValueError, AttributeError, OSError):
        pass  # not the main thread, or a platform without it

    period = 1.0 / FPS
    started = time.monotonic()
    try:
        while True:
            try:
                frame(time.monotonic() - started)
            except OSError:
                # The device vanishes across suspend; wait for it to come back
                # instead of dying.
                if not dev.reopen():
                    print("device did not come back", file=sys.stderr)
                    return 1
                dev.set_autonomous(False)
            time.sleep(period)
    except KeyboardInterrupt:
        print()
        return 0


# --------------------------------------------------------------------------
# Config file, for the colour that survives a reboot
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "color.conf")


def read_config() -> list[str]:
    try:
        with open(CONFIG) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line.split()
    except OSError:
        pass
    return ["blue"]


def write_config(args: list[str]) -> None:
    with open(CONFIG, "w") as fh:
        fh.write(
            "# Lighting applied at boot and after resume.\n"
            "# Same arguments you would pass to loq-kbd, for example:\n"
            "#   blue          azul 60          ff8800          mode3 40\n"
            + " ".join(args) + "\n"
        )


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

USAGE = """loq-kbd - fixed colours and effects for the Lenovo LOQ RGB keyboard

  loq-kbd <colour> [brightness]    a fixed colour, brightness 0-100
  loq-kbd <RRGGBB> [brightness]    a fixed colour by hex, # optional
  loq-kbd hex <RRGGBB> [bright]    the same, stated explicitly
  loq-kbd mode<N> [colour] [br]    a lighting mode, 1-5 (also modo<N>)
  loq-kbd auto                     give control back to the firmware
  loq-kbd off                      lights out
  loq-kbd save <args...>           remember these for boot and resume
  loq-kbd apply                    apply what was saved
  loq-kbd colours                  list every colour name
  loq-kbd modes                    list every mode
  loq-kbd info                     device facts
  loq-kbd zones                    per zone position and levels

Colour names work in English and Spanish: red or rojo, purple or morado.
Spanish command aliases: colores, modos, zonas, apagado, guardar, aplicar.
"""


def cmd_colours() -> int:
    print(f"{'english':<12} {'spanish':<14} hex")
    for hex_value, (en, es) in COLOR_NAMES.items():
        print(f"{en:<12} {es:<14} #{hex_value}")
    print("\nAny hex value works too: loq-kbd ff8800, loq-kbd '#f80'")
    return 0


def cmd_modes() -> int:
    print(f"{'n':<3} {'english':<11} {'spanish':<12} {'kind':<9} what it does")
    for num, (en, es, _f, animated, desc) in MODES.items():
        kind = "animated" if animated else "still"
        print(f"{num:<3} {en:<11} {es:<12} {kind:<9} {desc}")
    print("\nauto  hands control back to the firmware and its own effect")
    print("\nWrite a mode as mode3, modo3, m3, 3, wave or ola - all the same.")
    print("Animated modes run until Ctrl-C; still ones return immediately.")
    return 0


def cmd_info(dev: LampArray) -> int:
    a = dev.attributes
    kind = LAMP_ARRAY_KINDS.get(a["kind"], "unknown")
    print(f"{'device':<20} {dev.backend.description}")
    print(f"{'zones':<20} {a['lamps']}")
    print(f"{'kind':<20} {a['kind']} ({kind})")
    print(f"{'size':<20} {a['width_um']/1000:.0f} x {a['height_um']/1000:.0f} "
          f"x {a['depth_um']/1000:.0f} mm")
    print(f"{'min update interval':<20} {a['min_interval_us']} us "
          f"(max {1_000_000 // max(1, a['min_interval_us'])} fps)")
    z = dev.lamp(0)
    print(f"{'levels R/G/B':<20} "
          f"{z['red_levels']}/{z['green_levels']}/{z['blue_levels']}")
    print(f"{'intensity levels':<20} {z['intensity_levels']}")
    if z["intensity_levels"] <= 1:
        print(f"{'brightness':<20} Intensity field is inert; "
              f"emulated by scaling R,G,B")
    else:
        print(f"{'brightness':<20} Intensity field is honoured")
    return 0


def cmd_zones(dev: LampArray) -> int:
    print(f"{'id':>3}  {'X um':>7} {'Y um':>7} {'Z um':>6}  {'R/G/B':>11} "
          f"{'int':>4}  {'prog':>4} {'lat us':>6}  purpose")
    for i in range(dev.lamp_count):
        z = dev.lamp(i)
        levels = f"{z['red_levels']}/{z['green_levels']}/{z['blue_levels']}"
        purposes = ",".join(
            n for bit, n in LAMP_PURPOSES.items() if z["purposes"] & bit
        ) or f"0x{z['purposes']:x}"
        print(f"{z['id']:>3}  {z['x_um']:>7} {z['y_um']:>7} {z['z_um']:>6}  "
              f"{levels:>11} {z['intensity_levels']:>4}  "
              f"{'yes' if z['programmable'] else 'no':>4} "
              f"{z['latency_us']:>6}  {purposes}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help", "ayuda"):
        print(USAGE)
        return 0

    # These need no device.
    if argv[0] in ("colours", "colors", "colores"):
        return cmd_colours()
    if argv[0] in ("modes", "modos"):
        return cmd_modes()
    if argv[0] in ("save", "guardar"):
        if len(argv) < 2:
            print("save what? e.g. loq-kbd save mode3 60", file=sys.stderr)
            return 2
        write_config(argv[1:])
        print(f"saved to {CONFIG}: {' '.join(argv[1:])}")
        return 0

    if argv[0] in ("apply", "aplicar"):
        argv = read_config()
        if not argv:
            return 0

    try:
        dev = LampArray()
    except AccessDenied as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except DeviceNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        if argv[0] == "info":
            return cmd_info(dev)
        if argv[0] in ("zones", "zonas", "lamps", "lamparas"):
            return cmd_zones(dev)
        if argv[0] in ("auto", "firmware"):
            dev.set_autonomous(True)
            print("autonomous mode on: the firmware runs its own effect again.")
            return 0

        rest = list(argv)

        # hex is optional sugar; a bare hex value is accepted just the same.
        if rest[0].lower() in ("hex", "hexadecimal"):
            rest.pop(0)
            if not rest:
                print("hex what? e.g. loq-kbd hex ff8800", file=sys.stderr)
                return 2

        mode = resolve_mode(rest[0])
        if mode is not None:
            rest.pop(0)
        else:
            mode = 1

        rgb = (0, 0, 255)
        if rest:
            try:
                rgb = parse_color(rest[0])
                rest.pop(0)
            except ValueError:
                # Not a colour: maybe it is the brightness, e.g. "mode3 40".
                pass

        brightness = 100.0
        if rest:
            try:
                brightness = max(0.0, min(100.0, float(rest[0])))
                rest.pop(0)
            except ValueError:
                # A second colour is the likeliest mistake here, so say that
                # rather than the generic complaint.
                try:
                    parse_color(rest[0])
                except ValueError:
                    print(f"not a colour or a brightness: {rest[0]!r}. "
                          f"Try 'loq-kbd colours' or 'loq-kbd modes'.",
                          file=sys.stderr)
                else:
                    print(f"only one colour at a time, and {rest[0]!r} is a "
                          f"second one. Brightness goes last: loq-kbd red 60",
                          file=sys.stderr)
                return 2

        if rest:
            try:
                parse_color(rest[0])
            except ValueError:
                print(f"don't know what to do with: {' '.join(rest)}",
                      file=sys.stderr)
            else:
                print(f"only one colour at a time, and {rest[0]!r} is a second "
                      f"one. Brightness goes last: loq-kbd red 60",
                      file=sys.stderr)
            return 2

        return run_mode(dev, mode, rgb, brightness)
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

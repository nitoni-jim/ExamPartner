"""
services/fingerprint_service.py — normalisation, identity quality, match
strength and continuity for a PC's hardware reading (brief §4.F F1–F2,
§4.G, §4.H H1; contract §7).

EVERYTHING IN THIS FILE IS PROVISIONAL. The thresholds, the placeholder
list and the choice of signals were set from reasoning and from two
dissimilar machines, not from a lab of identical ones. The school
fingerprint survey (spec §7) exists to replace them, and spec §13 step 12
says they are frozen before the licensing work is called complete. Keep
them in this one module so that replacing them is one change.

The client sends RAW values (contract §7.1): untrimmed, placeholders
included. All normalisation happens here, so a matching fix is a server
deploy, not an update to every lab PC.

Two different questions, kept apart (F1) — revision 3 of the spec used
"confidence" for both:

- IDENTITY QUALITY of one reading — `strong` / `weak` / `degenerate`. It is
  what fingerprint_confidence records at claim time, and it depends on the
  POOL: an identifier carried by two or more active bindings in the pool is
  not usable, because it no longer tells those machines apart.
- MATCH STRENGTH between a reading and one binding — `strong` / `weak` /
  `mismatch` / `none`.

Matching is signal by signal, never by hash. identity_hash() exists for
the audit log and as an exact-match fast path only (brief §4.B).
"""
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional

# ---------------------------------------------------------------------------
# PROVISIONAL constants
# ---------------------------------------------------------------------------

# The survey collector's Clean-Value list, verbatim
# (tools/fingerprint-survey/Collect-Fingerprint.ps1), compared
# case-insensitively after trimming. Mirrored rather than reinvented, so the
# survey and the server agree on what "absent" means (brief §4.B). Expect it
# to grow from survey data; growing it is a server change only.
COLLECTOR_PLACEHOLDERS = frozenset({
    "to be filled by o.e.m.", "to be filled by oem", "default string",
    "none", "n/a", "na", "null", "unknown", "not applicable",
    "system serial number", "system manufacturer", "system product name",
    "base board serial number", "chassis serial number",
    "filled by oem", "oem", "0", "00000000", "not specified",
    "invalid", "empty",
})

# Counting runs that firmware vendors use as serial placeholders (brief §4.G).
DIGIT_RUN_PLACEHOLDERS = frozenset({"0123456789", "123456789", "1234567890"})

# Collection failures that make a reading unusable (contract §7.4). A class
# that could not be read is NOT an absent value: treating it as one would
# make a working PC look degenerate.
IDENTITY_CLASSES = ("Win32_ComputerSystemProduct", "Win32_BaseBoard", "Win32_BIOS")

# The identity hash covers exactly these, in this order (brief §4.G).
IDENTITY_HASH_FIELDS = (
    "smbios_uuid", "system_manufacturer", "system_model", "system_serial",
    "baseboard_manufacturer", "baseboard_product", "baseboard_serial", "bios_serial",
)

SERIAL_FIELDS = ("system_serial", "baseboard_serial", "bios_serial")

# String keys of contract §7.2 that must be a string or null.
_STRING_KEYS = (
    "smbios_uuid", "system_manufacturer", "system_model", "system_serial", "system_version",
    "baseboard_manufacturer", "baseboard_product", "baseboard_serial", "baseboard_version",
    "bios_manufacturer", "bios_version", "bios_serial", "bios_release_date",
    "cpu_name", "cpu_manufacturer", "cpu_processor_id", "windows_machine_guid",
)
_INT_KEYS = ("cpu_family", "cpu_cores", "cpu_logical", "total_physical_memory_bytes")

_HEX32 = re.compile(r"[0-9A-F]{32}")
_HEX12 = re.compile(r"[0-9A-F]{12}")


class SignalsInvalid(ValueError):
    """fingerprint_signals does not have the contract §7.2 shape. `field` names where."""

    def __init__(self, field_name: str, message: str):
        super().__init__(message)
        self.field = field_name


# ---------------------------------------------------------------------------
# Shape (contract §7.2) — checked before anything is normalised
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_signals(signals: Any) -> None:
    """Raise SignalsInvalid for anything that is not the §7.2 shape. Unknown keys are ignored."""
    prefix = "fingerprint_signals"
    if not isinstance(signals, dict):
        raise SignalsInvalid(prefix, "fingerprint_signals must be an object")
    if signals.get("schema") != 1 or not _is_int(signals.get("schema")):
        raise SignalsInvalid(f"{prefix}.schema", "fingerprint_signals.schema must be 1")
    for key in _STRING_KEYS:
        value = signals.get(key)
        if value is not None and not isinstance(value, str):
            raise SignalsInvalid(f"{prefix}.{key}", f"{key} must be a string or null")
    for key in _INT_KEYS:
        value = signals.get(key)
        if value is not None and not _is_int(value):
            raise SignalsInvalid(f"{prefix}.{key}", f"{key} must be an integer or null")
    for key, entry_strings in (
        ("disk_serials", ("model", "serial", "interface", "media_type", "pnp_device_id")),
        ("mac_addresses", ("mac", "permanent_address", "name", "pnp_device_id", "net_connection_id")),
    ):
        entries = signals.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise SignalsInvalid(f"{prefix}.{key}", f"{key} must be an array")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise SignalsInvalid(f"{prefix}.{key}[{i}]", f"{key} entries must be objects")
            for name in entry_strings:
                value = entry.get(name)
                if value is not None and not isinstance(value, str):
                    raise SignalsInvalid(f"{prefix}.{key}[{i}].{name}", f"{name} must be a string or null")
            if key == "disk_serials":
                if entry.get("is_system_disk") is not None and not isinstance(entry.get("is_system_disk"), bool):
                    raise SignalsInvalid(f"{prefix}.{key}[{i}].is_system_disk", "is_system_disk must be a boolean")
                for name in ("index", "size_bytes"):
                    if entry.get(name) is not None and not _is_int(entry.get(name)):
                        raise SignalsInvalid(f"{prefix}.{key}[{i}].{name}", f"{name} must be an integer or null")
    errors = signals.get("collection_errors")
    if errors is not None and (not isinstance(errors, list) or not all(isinstance(e, str) for e in errors)):
        raise SignalsInvalid(f"{prefix}.collection_errors", "collection_errors must be an array of strings")


def unreadable_classes(signals: Mapping[str, Any]) -> List[str]:
    """The identity classes collection_errors names. Non-empty → fingerprint_unreadable."""
    named = {str(e).strip().lower() for e in (signals.get("collection_errors") or [])}
    return [c for c in IDENTITY_CLASSES if c.lower() in named]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalise_value(value: Any) -> Optional[str]:
    """Trim and upper-case; None for anything that is absent wearing a costume."""
    if value is None or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.lower() in COLLECTOR_PLACEHOLDERS or text in DIGIT_RUN_PLACEHOLDERS:
        return None
    if len(set(text)) == 1:  # one repeated character: 0000000, FFFFFFFF, XXXXXXXX
        return None
    return text.upper()


def normalise_uuid(value: Any) -> Optional[str]:
    """32 upper-case hex digits, or None. Braces and hyphens are stripped first."""
    if value is None or not isinstance(value, str):
        return None
    if value.strip().lower() in COLLECTOR_PLACEHOLDERS:
        return None
    bare = re.sub(r"[{}\-\s]", "", value).upper()
    if not _HEX32.fullmatch(bare) or len(set(bare)) == 1:  # all-zeros and all-Fs are degenerate
        return None
    return bare


def normalise_mac(value: Any) -> Optional[str]:
    """
    12 upper-case hex digits, or None for all-zero and LOCALLY ADMINISTERED
    addresses (bit 0x02 of the first byte). Windows marks its random Wi-Fi
    hardware addresses that way, so they identify nothing.
    """
    if value is None or not isinstance(value, str):
        return None
    bare = re.sub(r"[:\-.\s]", "", value).upper()
    if not _HEX12.fullmatch(bare) or bare == "0" * 12:
        return None
    if int(bare[:2], 16) & 0x02:
        return None
    return bare


def _system_disk_serial(disks: Iterable[Any]) -> Optional[str]:
    """
    The serial of the ONE disk marked is_system_disk. None if no disk is
    marked or several are — never guess — and never a USB disk.
    """
    marked = [d for d in disks if isinstance(d, dict) and d.get("is_system_disk") is True]
    if len(marked) != 1:
        return None
    disk = marked[0]
    interface = (disk.get("interface") or "").strip().upper()
    pnp = (disk.get("pnp_device_id") or "").strip().upper()
    if interface == "USB" or pnp.startswith("USBSTOR\\") or pnp.startswith("USB\\"):
        return None
    return normalise_value(disk.get("serial"))


def _onboard_nic_addresses(adapters: Iterable[Any]) -> FrozenSet[str]:
    """Permanent (else current) addresses of adapters on the PCI bus — not USB, not virtual."""
    out = set()
    for adapter in adapters:
        if not isinstance(adapter, dict):
            continue
        if not (adapter.get("pnp_device_id") or "").strip().upper().startswith("PCI\\"):
            continue
        address = normalise_mac(adapter.get("permanent_address")) or normalise_mac(adapter.get("mac"))
        if address:
            out.add(address)
    return frozenset(out)


@dataclass(frozen=True)
class Reading:
    """A normalised reading. Pool-independent; usability is decided against a PoolIndex."""
    uuid: Optional[str]
    serials: FrozenSet[str]                     # distinct normalised system/baseboard/bios serials
    identity: Dict[str, str] = field(hash=False)  # IDENTITY_HASH_FIELDS → normalised value or ""
    activation_disk: Optional[str] = None       # system disk serial (activation continuity, H1)
    activation_nics: FrozenSet[str] = frozenset()  # onboard NIC addresses (activation continuity, H1)

    @property
    def activation_continuity(self) -> bool:
        return self.activation_disk is not None or bool(self.activation_nics)


def read(signals: Optional[Mapping[str, Any]]) -> Reading:
    """Normalise a raw §7.2 reading (or a stored snapshot). Tolerates None and partial data."""
    signals = signals if isinstance(signals, Mapping) else {}
    uuid = normalise_uuid(signals.get("smbios_uuid"))
    identity = {}
    for key in IDENTITY_HASH_FIELDS:
        value = uuid if key == "smbios_uuid" else normalise_value(signals.get(key))
        identity[key] = value or ""
    # A set: bios_serial equal to system_serial counts once, because on most
    # PCs it is the same number (F1).
    serials = frozenset(identity[k] for k in SERIAL_FIELDS if identity[k])
    return Reading(
        uuid=uuid,
        serials=serials,
        identity=identity,
        activation_disk=_system_disk_serial(signals.get("disk_serials") or []),
        activation_nics=_onboard_nic_addresses(signals.get("mac_addresses") or []),
    )


def read_stored(signals_json: Optional[str]) -> Reading:
    """A binding's stored snapshot, re-normalised with today's rules."""
    try:
        return read(json.loads(signals_json) if signals_json else None)
    except (TypeError, ValueError):
        return read(None)


# ---------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------

def identity_hash(reading: Reading) -> str:
    """SHA-256, lowercase hex, over k=v pairs joined with |, absent as empty (brief §4.G)."""
    text = "|".join(f"{k}={reading.identity[k]}" for k in IDENTITY_HASH_FIELDS)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_digest(pool_id: str, reading: Reading) -> str:
    """
    F4's activation_request_fp / request_fp: SHA-256 over the pool ID and the
    normalised reading, so the same request_id with a different pool or
    body is detectable. Computed once, when the request is first stored;
    never recomputed from stored signals (H5), whose normalisation will
    change as the placeholder list grows.
    """
    canonical = {
        "pool_id": pool_id,
        "identity": reading.identity,
        "activation_disk": reading.activation_disk or "",
        "activation_nics": sorted(reading.activation_nics),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Identity quality and match strength — relative to a pool (F1)
# ---------------------------------------------------------------------------

class PoolIndex:
    """How many ACTIVE bindings in a pool carry each UUID and each serial."""

    def __init__(self, binding_readings: Iterable[Reading]):
        self.uuid_counts: Counter = Counter()
        self.serial_counts: Counter = Counter()
        for r in binding_readings:
            if r.uuid:
                self.uuid_counts[r.uuid] += 1
            for s in r.serials:
                self.serial_counts[s] += 1

    def usable_uuid(self, reading: Reading) -> Optional[str]:
        # "not carried by two or more active bindings": the machine's own
        # binding carrying it once does not disqualify it.
        if reading.uuid and self.uuid_counts[reading.uuid] < 2:
            return reading.uuid
        return None

    def usable_serials(self, reading: Reading) -> FrozenSet[str]:
        return frozenset(s for s in reading.serials if self.serial_counts[s] < 2)


def identity_quality(reading: Reading, pool: PoolIndex) -> str:
    has_uuid = pool.usable_uuid(reading) is not None
    has_serial = bool(pool.usable_serials(reading))
    if has_uuid and has_serial:
        return "strong"
    if has_uuid or has_serial:
        return "weak"
    return "degenerate"


def match_strength(reading: Reading, binding: Reading, pool: PoolIndex) -> str:
    r_uuid, b_uuid = pool.usable_uuid(reading), pool.usable_uuid(binding)
    same_uuid = r_uuid is not None and r_uuid == b_uuid
    shared_serial = bool(pool.usable_serials(reading) & pool.usable_serials(binding))
    if same_uuid and shared_serial:
        return "strong"
    if same_uuid or shared_serial:
        return "weak"
    if r_uuid is not None and b_uuid is not None:
        return "mismatch"  # both usable, different, and no shared serial
    return "none"


def activation_continuity_match(reading: Reading, binding: Reading) -> bool:
    """
    F2 / H1: does this reading share the system disk serial or an onboard
    network card's permanent address with the binding? Activation only —
    MachineGuid is deliberately NOT here: unsysprepped images share it across
    a lab, and a reinstall changes it. It belongs to the refresh
    installation-continuity rule (brief §4.A, Unit 3).
    """
    if reading.activation_disk and reading.activation_disk == binding.activation_disk:
        return True
    return bool(reading.activation_nics & binding.activation_nics)

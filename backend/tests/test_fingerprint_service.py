"""
tests/test_fingerprint_service.py — the provisional identity rules
(brief §4.F F1–F2, §4.G, §4.H H1).

Continuity tests use constructed readings, not survey files: the current
collector does not record is_system_disk, pnp_device_id or
permanent_address (brief §4.G), so a survey file cannot exercise them.
"""
import re

import pytest

from conftest import make_degenerate_signals, make_signals


def _fs():
    from services import fingerprint_service

    return fingerprint_service


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _collector_placeholders():
    """The list as it appears in the collector itself, so drift between the
    two is caught here rather than in a lab."""
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "fingerprint-survey", "Collect-Fingerprint.ps1")
    text = open(path, encoding="utf-8").read()
    block = text[text.index("$placeholders = @("):text.index(")", text.index("$placeholders = @("))]
    return re.findall(r"'([^']*)'", block)


def test_the_server_list_is_the_collectors_list():
    assert set(_collector_placeholders()) == set(_fs().COLLECTOR_PLACEHOLDERS)


@pytest.mark.parametrize("placeholder", sorted([
    "to be filled by o.e.m.", "to be filled by oem", "default string", "none", "n/a", "na", "null",
    "unknown", "not applicable", "system serial number", "system manufacturer", "system product name",
    "base board serial number", "chassis serial number", "filled by oem", "oem", "0", "00000000",
    "not specified", "invalid", "empty",
]))
def test_every_collector_placeholder_maps_to_absent_in_any_case(placeholder):
    fs = _fs()
    for variant in (placeholder, placeholder.upper(), placeholder.title(), f"  {placeholder}\t"):
        assert fs.normalise_value(variant) is None


@pytest.mark.parametrize("value", ["0000000", "FFFFFFFF", "XXXXXXXX", "xxxx", "1", "-----", "  ZZZ  "])
def test_repeated_character_values_are_absent(value):
    assert _fs().normalise_value(value) is None


@pytest.mark.parametrize("value", ["0123456789", "123456789", "1234567890"])
def test_digit_runs_are_absent(value):
    assert _fs().normalise_value(value) is None


def test_real_values_are_trimmed_and_upper_cased():
    fs = _fs()
    assert fs.normalise_value("  b5qmt32 ") == "B5QMT32"
    assert fs.normalise_value("12345678") == "12345678"   # a real-looking serial survives
    assert fs.normalise_value(None) is None
    assert fs.normalise_value("") is None


@pytest.mark.parametrize("uuid, expected", [
    ("4c4c4544-0042-3510-8051-b4c04f4d3732", "4C4C4544004235108051B4C04F4D3732"),
    ("{4C4C4544-0042-3510-8051-B4C04F4D3732}", "4C4C4544004235108051B4C04F4D3732"),
    ("00000000-0000-0000-0000-000000000000", None),
    ("FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF", None),
    ("ffffffff-ffff-ffff-ffff-ffffffffffff", None),
    ("To Be Filled By O.E.M.", None),
    ("4C4C4544-0042-3510-8051", None),                    # too short
    ("4C4C4544-0042-3510-8051-B4C04F4D373Z", None),       # not hex
    (None, None),
])
def test_uuid_degenerate_forms(uuid, expected):
    assert _fs().normalise_uuid(uuid) == expected


@pytest.mark.parametrize("mac, expected", [
    ("B4:45:06:12:34:56", "B44506123456"),
    ("b4-45-06-12-34-56", "B44506123456"),
    ("000000000000", None),                 # all zero
    ("02:11:22:33:44:55", None),            # locally administered (0x02)
    ("DA:A1:19:00:00:01", None),            # 0xDA has bit 0x02 set: Windows random Wi-Fi address
    ("B4:45:06:12:34", None),               # too short
])
def test_mac_normalisation(mac, expected):
    assert _fs().normalise_mac(mac) == expected


def test_bios_serial_equal_to_system_serial_counts_once():
    r = _fs().read(make_signals(serial="ABC123", board_serial="Default string", bios_serial="abc123 "))
    assert r.serials == frozenset({"ABC123"})


# ---------------------------------------------------------------------------
# Continuity signals at activation (H1)
# ---------------------------------------------------------------------------

def _disk(serial, system=True, interface="SCSI", pnp="SCSI\\DISK&VEN_X\\1"):
    return {"index": 0, "model": "M", "serial": serial, "interface": interface, "media_type": "Fixed hard disk media",
            "pnp_device_id": pnp, "size_bytes": 1, "is_system_disk": system}


def test_system_disk_is_the_one_marked_disk():
    r = _fs().read(make_signals(disk_serials=[_disk("USBDISK1", system=False), _disk(" sys-disk-1 ")]))
    assert r.activation_disk == "SYS-DISK-1"


@pytest.mark.parametrize("disks", [
    [_disk("A", system=False), _disk("B", system=False)],             # none marked
    [_disk("A"), _disk("B")],                                         # two marked: never guess
    [_disk("USB1", interface="USB")],                                 # a USB disk is never the system disk
    [_disk("USB2", pnp="USBSTOR\\DISK&VEN_SANDISK\\1")],
    [_disk("To Be Filled By O.E.M.")],                                # placeholder serial
])
def test_no_system_disk_is_guessed(disks):
    assert _fs().read(make_signals(disk_serials=disks)).activation_disk is None


def test_only_onboard_pci_adapters_count_and_permanent_address_wins():
    adapters = [
        {"mac": "B4:45:06:12:34:56", "permanent_address": "B4-45-06-AA-BB-CC", "pnp_device_id": "PCI\\VEN_10EC"},
        {"mac": "00:E0:4C:68:00:01", "permanent_address": None, "pnp_device_id": "USB\\VID_0BDA"},       # USB adapter
        {"mac": "00:15:5D:00:00:01", "permanent_address": None, "pnp_device_id": "ROOT\\VMS_MP\\0000"},  # virtual
        {"mac": "DA:A1:19:00:00:01", "permanent_address": None, "pnp_device_id": "PCI\\VEN_8086"},       # random Wi-Fi
        {"mac": "3C:A9:F4:00:00:02", "permanent_address": None, "pnp_device_id": "pci\\ven_8086"},       # falls back to mac
    ]
    assert _fs().read(make_signals(mac_addresses=adapters)).activation_nics == frozenset({"B44506AABBCC", "3CA9F4000002"})


def test_machine_guid_is_not_an_activation_continuity_signal():
    """H1: two different PCs from one unsysprepped image share MachineGuid."""
    fs = _fs()
    a = fs.read(make_degenerate_signals(disk="DISK-A", guid="11111111-2222-3333-4444-555555555555"))
    b = fs.read(make_degenerate_signals(disk="DISK-B", guid="11111111-2222-3333-4444-555555555555"))
    assert not fs.activation_continuity_match(a, b)


def test_continuity_matches_on_disk_or_nic():
    fs = _fs()
    base = fs.read(make_degenerate_signals(disk="DISK-A", nic="B44506000001"))
    assert fs.activation_continuity_match(fs.read(make_degenerate_signals(disk="DISK-A", nic=None)), base)
    assert fs.activation_continuity_match(fs.read(make_degenerate_signals(disk=None, nic="B44506000001")), base)
    assert not fs.activation_continuity_match(fs.read(make_degenerate_signals(disk="DISK-B", nic="B44506000002")), base)


# ---------------------------------------------------------------------------
# Identity quality and match strength (F1)
# ---------------------------------------------------------------------------

def _pool(*signal_sets):
    fs = _fs()
    return fs.PoolIndex([fs.read(s) for s in signal_sets])


def test_identity_quality_levels():
    fs = _fs()
    empty = fs.PoolIndex([])
    assert fs.identity_quality(fs.read(make_signals()), empty) == "strong"
    assert fs.identity_quality(fs.read(make_signals(serial="Default string", board_serial=None)), empty) == "weak"
    assert fs.identity_quality(fs.read(make_signals(uuid=None)), empty) == "weak"
    assert fs.identity_quality(fs.read(make_degenerate_signals(disk="D")), empty) == "degenerate"


def test_identifiers_shared_by_two_active_bindings_are_not_usable():
    fs = _fs()
    shared_uuid = "11111111-2222-3333-4444-555555555555"
    pool = _pool(make_signals(uuid=shared_uuid, serial="S1", board_serial=None),
                 make_signals(uuid=shared_uuid, serial="S2", board_serial=None))
    r = fs.read(make_signals(uuid=shared_uuid, serial="S3", board_serial=None))
    assert pool.usable_uuid(r) is None
    assert fs.identity_quality(r, pool) == "weak"
    # One binding carrying it — the machine's own, on a reinstall — is fine.
    one = _pool(make_signals(uuid=shared_uuid, serial="S1", board_serial=None))
    assert one.usable_uuid(r) is not None


@pytest.mark.parametrize("reading_kwargs, expected", [
    ({}, "strong"),                                                        # same UUID, shared serial
    ({"serial": "OTHER1", "board_serial": "OTHER2"}, "weak"),              # same UUID only
    ({"uuid": "11111111-2222-3333-4444-555555555555"}, "weak"),            # shared serial only
    ({"uuid": "11111111-2222-3333-4444-555555555555", "serial": "OTHER1", "board_serial": "OTHER2"}, "mismatch"),
    ({"uuid": None, "serial": "OTHER1", "board_serial": "OTHER2"}, "none"),  # no usable UUID on one side
])
def test_match_strength(reading_kwargs, expected):
    fs = _fs()
    stored = make_signals()
    pool = _pool(stored)
    assert fs.match_strength(fs.read(make_signals(**reading_kwargs)), fs.read(stored), pool) == expected


# ---------------------------------------------------------------------------
# Hashes and shape
# ---------------------------------------------------------------------------

def test_identity_hash_ignores_cosmetic_differences_and_is_lowercase_hex():
    fs = _fs()
    a = fs.identity_hash(fs.read(make_signals()))
    b = fs.identity_hash(fs.read(make_signals(serial="  b5qmt32 ", uuid="{4c4c4544-0042-3510-8051-b4c04f4d3732}")))
    assert a == b and re.fullmatch(r"[0-9a-f]{64}", a)


def test_request_digest_depends_on_pool_and_reading():
    fs = _fs()
    r = fs.read(make_signals())
    assert fs.request_digest("pool-a", r) == fs.request_digest("pool-a", fs.read(make_signals()))
    assert fs.request_digest("pool-a", r) != fs.request_digest("pool-b", r)
    assert fs.request_digest("pool-a", r) != fs.request_digest("pool-a", fs.read(make_signals(disk="OTHER")))


@pytest.mark.parametrize("errors, expected", [
    ([], []),
    (["MachineGuid", "Win32_Processor"], []),
    (["Win32_BIOS"], ["Win32_BIOS"]),
    (["win32_baseboard", "Win32_ComputerSystemProduct"], ["Win32_ComputerSystemProduct", "Win32_BaseBoard"]),
])
def test_unreadable_classes(errors, expected):
    assert _fs().unreadable_classes(make_signals(collection_errors=errors)) == expected


@pytest.mark.parametrize("change, field", [
    ({"schema": 2}, "fingerprint_signals.schema"),
    ({"smbios_uuid": 123}, "fingerprint_signals.smbios_uuid"),
    ({"cpu_cores": "6"}, "fingerprint_signals.cpu_cores"),
    ({"disk_serials": {}}, "fingerprint_signals.disk_serials"),
    ({"disk_serials": [{"serial": 5}]}, "fingerprint_signals.disk_serials[0].serial"),
    ({"disk_serials": [{"is_system_disk": "yes"}]}, "fingerprint_signals.disk_serials[0].is_system_disk"),
    ({"mac_addresses": ["B4:45"]}, "fingerprint_signals.mac_addresses[0]"),
    ({"collection_errors": "Win32_BIOS"}, "fingerprint_signals.collection_errors"),
])
def test_validate_signals_names_the_field(change, field):
    fs = _fs()
    with pytest.raises(fs.SignalsInvalid) as exc:
        fs.validate_signals(make_signals(**change))
    assert exc.value.field == field


def test_unknown_keys_are_ignored():
    _fs().validate_signals(make_signals(something_new={"a": 1}))

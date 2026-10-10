<#
    ExamPartner — hardware fingerprint survey collector
    ===================================================

    READ-ONLY MEASUREMENT TOOL. This script does not activate ExamPartner,
    does not contact any server, does not consume a licence seat, and writes
    nothing outside its own output folder. It reads hardware identifiers that
    Windows already exposes and saves them to a file.

    PURPOSE
    Before the ExamPartner Windows licensing design freezes its machine-
    matching rules, we need to know what real Nigerian school hardware
    actually reports. Specifically:

      - Are SMBIOS UUIDs unique across a batch of identically-purchased PCs?
      - Are any blank, all-zeros or all-FFs? (The SMBIOS standard permits a
        firmware to report a null UUID when none is available, and Windows
        surfaces that value as-is.)
      - Are baseboard / system serials present, or empty strings?
      - Which combination of signals actually distinguishes these machines?

    That last question cannot be answered by reasoning. It has to be measured
    on the machines in question, which is what this script is for.

    WHY POWERSHELL rather than a compiled tool: no build step, no .NET runtime
    to install on a school PC, readable by the IT administrator who runs it,
    and it works from a USB stick on any Windows 10 or 11 machine.

    DOES NOT REQUIRE ADMINISTRATOR. Every value below is readable by a
    standard user. If you are prompted to elevate, something else is wrong.

    USAGE
        Double-click Run-Collector.bat, or:
        powershell -ExecutionPolicy Bypass -File Collect-Fingerprint.ps1

    OUTPUT
        .\survey-output\<label>_<timestamp>.json   one file per machine
        .\survey-output\survey-summary.csv         one row per machine

    Send the whole survey-output folder back when you are done.
#>

[CmdletBinding()]
param(
    # Machine label, e.g. LAB-PC-07. Prompted for if not supplied.
    [string]$Label,

    # Where to write results. Defaults to a folder beside this script, so a
    # USB stick collects every machine's output in one place.
    [string]$OutputDir
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Classes that could not be read, and why. See the comment in Get-Cim: this
# list is the difference between a hardware finding and a broken measurement.
$script:CimFailures = [ordered]@{}

function Get-Cim {
    <#
        Wraps Get-CimInstance so one unavailable class cannot abort the run.

        CRITICAL: a failed read is NOT the same as an absent value, and the two
        must never be conflated. "This machine reports no baseboard serial" is a
        finding that would shape the licensing design. "WMI would not answer" is
        a broken instrument. If WMI is disabled, restricted by policy, or its
        repository is corrupted, an earlier version of this script recorded the
        result as 'no usable identifiers' — which, across a whole lab, would
        have looked like conclusive evidence that the hardware was
        unfingerprintable and driven exactly the wrong design decision.

        Failures are therefore recorded, and the diagnostics block downgrades
        its verdict to 'unknown' rather than 'False' when any identity-critical
        class could not be read.
    #>
    param([string]$Class)
    try {
        Get-CimInstance -ClassName $Class -ErrorAction Stop
    } catch {
        Write-Warning "Could not read $Class : $($_.Exception.Message)"
        $script:CimFailures[$Class] = $_.Exception.Message
        $null
    }
}

function Clean-Value {
    <#
        Normalises a raw firmware string for reporting.

        Firmware routinely returns placeholder text instead of leaving a
        field empty — "To Be Filled By O.E.M.", "Default string", "None",
        "System Serial Number". Those are absent values wearing a costume,
        and treating them as real identifiers is exactly the mistake this
        survey exists to prevent. They are mapped to $null and counted as
        missing.
    #>
    param($Value)
    if ($null -eq $Value) { return $null }
    $s = ([string]$Value).Trim()
    if ($s -eq '') { return $null }

    $placeholders = @(
        'to be filled by o.e.m.', 'to be filled by oem', 'default string',
        'none', 'n/a', 'na', 'null', 'unknown', 'not applicable',
        'system serial number', 'system manufacturer', 'system product name',
        'base board serial number', 'chassis serial number',
        'filled by oem', 'oem', '0', '00000000', 'not specified',
        'invalid', 'empty'
    )
    if ($placeholders -contains $s.ToLower()) { return $null }
    return $s
}

function Test-DegenerateUuid {
    <#
        Classifies an SMBIOS UUID. 'degenerate' means the value is present but
        carries no identifying information, which is materially different from
        'missing' and from 'usable' — and is the case that would silently break
        machine matching across a lab of identical PCs.
    #>
    param($Uuid)
    $u = Clean-Value $Uuid
    if ($null -eq $u) { return 'missing' }

    $bare = ($u -replace '[{}\-]', '').ToUpper()
    if ($bare.Length -ne 32)        { return 'malformed' }
    if ($bare -eq ('0' * 32))       { return 'degenerate_all_zeros' }
    if ($bare -eq ('F' * 32))       { return 'degenerate_all_ffs' }
    # A handful of firmwares ship a single hard-coded UUID across a whole
    # production batch. We cannot detect that from one machine — it only shows
    # up when the summary CSV is compared across the lab. Flagged there.
    return 'usable'
}

# ---------------------------------------------------------------------------
# Label
# ---------------------------------------------------------------------------

if (-not $Label) {
    Write-Host ''
    Write-Host '  ExamPartner hardware survey (read-only)' -ForegroundColor Cyan
    Write-Host '  Nothing is installed, activated or sent anywhere.'
    Write-Host ''
    $Label = Read-Host '  Machine label (e.g. LAB-PC-07)'
}
$Label = ($Label -replace '[^A-Za-z0-9_\-\.]', '_').Trim('_')
if (-not $Label) { $Label = "UNLABELLED" }

if (-not $OutputDir) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
    $OutputDir = Join-Path $scriptDir 'survey-output'
}
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

Write-Host ''
Write-Host "  Collecting from '$Label' ..." -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------

$product   = Get-Cim 'Win32_ComputerSystemProduct'
$board     = Get-Cim 'Win32_BaseBoard'
$bios      = Get-Cim 'Win32_BIOS'
$cpu       = @(Get-Cim 'Win32_Processor')[0]
$system    = Get-Cim 'Win32_ComputerSystem'
$os        = Get-Cim 'Win32_OperatingSystem'
$disks     = @(Get-Cim 'Win32_DiskDrive')
$adapters  = @(Get-Cim 'Win32_NetworkAdapter' | Where-Object { $_.PhysicalAdapter -and $_.MACAddress })

# MachineGuid is generated at Windows install time, so it changes on reinstall.
# Collected to demonstrate that it is unsuitable as a physical machine
# identity, not as a candidate for one.
$machineGuid = $null
try {
    $machineGuid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid -ErrorAction Stop).MachineGuid
} catch { }

$winInstall = $null
try {
    $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop
    $winInstall = [ordered]@{
        product_id   = Clean-Value $cv.ProductId
        install_date = if ($cv.InstallDate) {
            ([datetime]'1970-01-01Z').AddSeconds([int64]$cv.InstallDate).ToString('s')
        } else { $null }
        display_version = Clean-Value $cv.DisplayVersion
        build           = Clean-Value $cv.CurrentBuild
    }
} catch { }

# --- The candidates -------------------------------------------------------
# These are the comparatively stable, firmware-held values that a composite
# machine fingerprint would actually be built from.
$candidates = [ordered]@{
    smbios_uuid          = Clean-Value $product.UUID
    system_manufacturer  = Clean-Value $product.Vendor
    system_model         = Clean-Value $product.Name
    system_serial        = Clean-Value $product.IdentifyingNumber
    system_version       = Clean-Value $product.Version
    baseboard_manufacturer = Clean-Value $board.Manufacturer
    baseboard_product    = Clean-Value $board.Product
    baseboard_serial     = Clean-Value $board.SerialNumber
    baseboard_version    = Clean-Value $board.Version
    bios_manufacturer    = Clean-Value $bios.Manufacturer
    bios_version         = Clean-Value $bios.SMBIOSBIOSVersion
    bios_serial          = Clean-Value $bios.SerialNumber
    bios_release_date    = if ($bios.ReleaseDate) { $bios.ReleaseDate.ToString('s') } else { $null }
}

# --- Supporting, NOT identifying -----------------------------------------
# Modern x86 exposes no per-CPU serial (processor serial number was disabled
# after the Pentium III). Win32_Processor.ProcessorId is a CPUID signature
# plus feature flags, so every PC in a batch of identical machines reports
# the SAME value. Collected specifically to confirm that, so the decision to
# treat CPU data as non-identifying rests on measurement.
$supporting = [ordered]@{
    cpu_name              = Clean-Value $cpu.Name
    cpu_manufacturer      = Clean-Value $cpu.Manufacturer
    cpu_processor_id      = Clean-Value $cpu.ProcessorId
    cpu_family            = $cpu.Family
    cpu_cores             = $cpu.NumberOfCores
    cpu_logical           = $cpu.NumberOfLogicalProcessors
}

# --- Collected to demonstrate unsuitability ------------------------------
# Each of these changes under ordinary maintenance or is administrator-
# editable. Recorded so the survey can show that, rather than asserting it.
$unsuitable = [ordered]@{
    windows_machine_guid = Clean-Value $machineGuid      # changes on reinstall
    windows_install      = $winInstall                   # changes on reinstall
    computer_name        = Clean-Value $system.Name      # admin-editable
    domain               = Clean-Value $system.Domain
    total_physical_memory_bytes = $system.TotalPhysicalMemory  # changes on upgrade
    disk_serials = @(
        $disks | ForEach-Object {
            [ordered]@{
                model     = Clean-Value $_.Model
                serial    = Clean-Value $_.SerialNumber   # changes on disk swap
                interface = Clean-Value $_.InterfaceType
                size_bytes = $_.Size
            }
        }
    )
    mac_addresses = @(
        $adapters | ForEach-Object {
            [ordered]@{
                name = Clean-Value $_.Name
                mac  = Clean-Value $_.MACAddress          # adapters change
            }
        }
    )
}

# --- Diagnostics ----------------------------------------------------------
$presentCandidates = @($candidates.Keys | Where-Object { $null -ne $candidates[$_] })
$uuidVerdict = Test-DegenerateUuid $product.UUID

# The strong signals are the ones a composite fingerprint would weight most.
# If fewer than two are usable, this machine cannot be reliably distinguished
# from an identical sibling by firmware alone, and the licensing design must
# fall back to installation identity plus administrator confirmation.
#
# The UUID only counts when it is actually usable. An all-zeros UUID is a
# non-null string, so a naive presence test counts it as a strong signal while
# it identifies nothing — which overstated identifiability on precisely the
# machines where the number matters. Present-but-worthless is not a signal.
$strongSignals = @(
    @('smbios_uuid', 'baseboard_serial', 'system_serial', 'bios_serial') |
        Where-Object {
            ($null -ne $candidates[$_]) -and
            -not (($_ -eq 'smbios_uuid') -and ($uuidVerdict -ne 'usable'))
        }
)

# Identity findings are only meaningful if the instrument worked. These three
# classes carry every candidate signal; if any failed to read, this machine's
# row says nothing about its hardware and must not be counted as evidence.
$identityCritical = @('Win32_ComputerSystemProduct', 'Win32_BaseBoard', 'Win32_BIOS')
$failedCritical   = @($identityCritical | Where-Object { $script:CimFailures.Contains($_) })
$collectionOk     = ($failedCritical.Count -eq 0)

$diagnostics = [ordered]@{
    collection_status       = if ($collectionOk) { 'complete' } else { 'INCOMPLETE' }
    classes_failed          = @($script:CimFailures.Keys)
    uuid_verdict            = if ($collectionOk) { $uuidVerdict } else { 'unknown_read_failed' }
    candidates_present      = $presentCandidates.Count
    candidates_total        = $candidates.Count
    strong_signals_present  = $strongSignals.Count
    strong_signals          = $strongSignals -join '|'
    # 'unknown' rather than False when the instrument failed. A false negative
    # here would be read as a hardware finding across the whole lab.
    fingerprintable         = if (-not $collectionOk) { 'unknown' }
                              elseif (($uuidVerdict -eq 'usable') -or ($strongSignals.Count -ge 2)) { 'True' }
                              else { 'False' }
    notes                   = @()
}

if (-not $collectionOk) {
    $diagnostics.notes += "COLLECTION FAILED for $($failedCritical -join ', '). This row is NOT evidence about the hardware - it means the reading could not be taken. Re-run, and check that the 'Windows Management Instrumentation' service is running."
} else {
    if ($uuidVerdict -ne 'usable') {
        $diagnostics.notes += "SMBIOS UUID is $uuidVerdict - cannot be the primary identity on this machine."
    }
    if ($strongSignals.Count -lt 2) {
        $diagnostics.notes += "Fewer than two strong firmware signals present - degenerate identity risk."
    }
    if ($null -eq $candidates.baseboard_serial -and $null -eq $candidates.system_serial) {
        $diagnostics.notes += "Neither baseboard nor system serial is populated - common on low-cost OEM boards."
    }
}

# ---------------------------------------------------------------------------
# Write output
# ---------------------------------------------------------------------------

$record = [ordered]@{
    schema_version   = 1
    machine_label    = $Label
    collected_at_utc = (Get-Date).ToUniversalTime().ToString('s') + 'Z'
    collector        = 'ExamPartner survey collector (read-only)'
    os_caption       = Clean-Value $os.Caption
    os_version       = Clean-Value $os.Version
    powershell       = $PSVersionTable.PSVersion.ToString()
    candidate_identity_signals = $candidates
    supporting_non_identifying = $supporting
    collected_to_show_unsuitable = $unsuitable
    diagnostics      = $diagnostics
}

$stamp    = (Get-Date).ToString('yyyyMMdd-HHmmss')
$jsonPath = Join-Path $OutputDir "$Label`_$stamp.json"
$record | ConvertTo-Json -Depth 8 | Set-Content -Path $jsonPath -Encoding UTF8

# One flat CSV row per machine. This is the artefact that answers the survey's
# real question, because uniqueness is only visible by comparing machines —
# no single machine's JSON can reveal a duplicated UUID.
$csvPath = Join-Path $OutputDir 'survey-summary.csv'
$row = [ordered]@{
    machine_label          = $Label
    collected_at_utc       = $record.collected_at_utc
    collection_status      = $diagnostics.collection_status
    uuid_verdict           = $diagnostics.uuid_verdict
    fingerprintable        = $diagnostics.fingerprintable
    strong_signals_present = $diagnostics.strong_signals_present
    smbios_uuid            = $candidates.smbios_uuid
    system_manufacturer    = $candidates.system_manufacturer
    system_model           = $candidates.system_model
    system_serial          = $candidates.system_serial
    baseboard_manufacturer = $candidates.baseboard_manufacturer
    baseboard_product      = $candidates.baseboard_product
    baseboard_serial       = $candidates.baseboard_serial
    bios_manufacturer      = $candidates.bios_manufacturer
    bios_version           = $candidates.bios_version
    bios_serial            = $candidates.bios_serial
    bios_release_date      = $candidates.bios_release_date
    cpu_name               = $supporting.cpu_name
    cpu_processor_id       = $supporting.cpu_processor_id
    windows_machine_guid   = $unsuitable.windows_machine_guid
    computer_name          = $unsuitable.computer_name
    total_memory_bytes     = $unsuitable.total_physical_memory_bytes
    first_disk_serial      = if ($unsuitable.disk_serials.Count) { $unsuitable.disk_serials[0].serial } else { $null }
    first_mac              = if ($unsuitable.mac_addresses.Count) { $unsuitable.mac_addresses[0].mac } else { $null }
    os_caption             = $record.os_caption
}
[pscustomobject]$row | Export-Csv -Path $csvPath -NoTypeInformation -Append -Encoding UTF8

# ---------------------------------------------------------------------------
# Report to the person standing at the machine
# ---------------------------------------------------------------------------

Write-Host ''
if ($collectionOk) {
    Write-Host "  Done: $Label" -ForegroundColor Green
} else {
    Write-Host "  PROBLEM: $Label - the reading could not be taken" -ForegroundColor Red
}
Write-Host "    Collection        : $($diagnostics.collection_status)"
Write-Host "    SMBIOS UUID       : $($diagnostics.uuid_verdict)"
Write-Host "    Strong signals    : $($diagnostics.strong_signals_present) of 4  ($($diagnostics.strong_signals))"
Write-Host "    Fingerprintable   : $($diagnostics.fingerprintable)"
foreach ($n in $diagnostics.notes) {
    Write-Host "    ! $n" -ForegroundColor $(if ($collectionOk) { 'Yellow' } else { 'Red' })
}
Write-Host ''
Write-Host "  Saved to $OutputDir"
if ($collectionOk) {
    Write-Host '  Run this on the next PC, then send the whole survey-output folder back.'
} else {
    Write-Host '  Please tell Nitoni this machine reported a collection failure.' -ForegroundColor Red
}
Write-Host ''

if (-not $env:EXAMPARTNER_NO_PAUSE) {
    Read-Host '  Press Enter to close'
}

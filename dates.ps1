# Prints "name<TAB>unix-seconds" for each filename listed in $List, using the
# Windows shell's Date Taken / Date Encoded properties (works on iCloud
# cloud-only placeholders without downloading them).
param([string]$Dir, [string]$List)
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ns = (New-Object -ComObject Shell.Application).Namespace($Dir)
foreach ($name in [IO.File]::ReadAllLines($List, [Text.Encoding]::UTF8)) {
    if (-not $name) { continue }
    $it = $ns.ParseName($name)
    if (-not $it) { continue }
    $dt = $it.ExtendedProperty('System.Photo.DateTaken')
    if (-not $dt) { $dt = $it.ExtendedProperty('System.Media.DateEncoded') }
    if ($dt) {
        $s = ([DateTimeOffset]$dt.ToUniversalTime()).ToUnixTimeSeconds()
        "$name`t$s"
    }
}

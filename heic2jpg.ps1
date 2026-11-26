# Converts a HEIC/HEIF to a downscaled JPEG using Windows' built-in WIC codec.
param([string]$In, [string]$Out, [int]$Max = 1800)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationCore
$dec = [Windows.Media.Imaging.BitmapDecoder]::Create([Uri]$In, 'IgnoreColorProfile', 'OnLoad')
$src = $dec.Frames[0]
$scale = [Math]::Min(1.0, $Max / [Math]::Max($src.PixelWidth, $src.PixelHeight))
$img = $src
if ($scale -lt 1.0) {
    $img = New-Object Windows.Media.Imaging.TransformedBitmap($src, (New-Object Windows.Media.ScaleTransform($scale, $scale)))
}
$enc = New-Object Windows.Media.Imaging.JpegBitmapEncoder
$enc.QualityLevel = 85
$enc.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($img))
$fs = [IO.File]::Create($Out)
try { $enc.Save($fs) } finally { $fs.Close() }

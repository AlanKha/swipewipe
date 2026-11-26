# Long-running helper: reads "<source>`t<output.jpg>`t<size>" lines on stdin and writes the
# Windows shell thumbnail for each. iCloud supplies these for cloud-only photos
# without downloading the original. Prints "ok" or "err <message>" per line.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Runtime.InteropServices;
public static class ShellThumb {
  [ComImport, Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IShellItemImageFactory { void GetImage(SIZE size, int flags, out IntPtr phbm); }
  [StructLayout(LayoutKind.Sequential)] struct SIZE { public int cx, cy; public SIZE(int x, int y) { cx = x; cy = y; } }
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHCreateItemFromParsingName(string path, IntPtr pbc, [MarshalAs(UnmanagedType.LPStruct)] Guid riid, [MarshalAs(UnmanagedType.Interface)] out IShellItemImageFactory ppv);
  [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr h);
  // flags: 0x1 BIGGERSIZEOK | 0x8 THUMBNAILONLY (never fall back to a file-type icon)
  public static void Save(string path, string output, int size) {
    IShellItemImageFactory f;
    SHCreateItemFromParsingName(path, IntPtr.Zero, new Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"), out f);
    IntPtr hbm; f.GetImage(new SIZE(size, size), 0x9, out hbm);
    try { using (Bitmap b = Image.FromHbitmap(hbm)) b.Save(output, System.Drawing.Imaging.ImageFormat.Jpeg); }
    finally { DeleteObject(hbm); }
  }
}
'@
while ($null -ne ($line = [Console]::In.ReadLine())) {
    $parts = $line.Split("`t")
    try {
        [ShellThumb]::Save($parts[0], $parts[1], [int]$parts[2])
        [Console]::Out.WriteLine("ok")
    } catch {
        [Console]::Out.WriteLine("err " + ($_.Exception.Message -replace "`r?`n", " "))
    }
    [Console]::Out.Flush()
}

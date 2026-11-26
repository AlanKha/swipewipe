# SwipeWipe (desktop)

A personal desktop take on the swipe-to-clean-your-camera-roll idea. It runs locally and works with iCloud Photos on Windows and macOS.

- Swipe left to bin and right to keep, with the mouse or the ← / → keys. **↓** (or **Z**) undoes and **Esc** goes back home.
- Home screen sets: **recents** (last 30 days), **random**, **duplicates**, **no date**, and one band per month.
- Each set shows its progress, and a month is marked complete once every photo in it is swiped. You can also mark a month done by hand.
- Nothing is deleted until you confirm in **Review bin**, and even then you get a 10-second undo.

## Duplicates

SwipeWipe treats photos as duplicates when they have the exact same file size and date taken. Re-imports, such as a Google Photos migration, often leave copies named `IMG_1234(1).JPG` next to the original.

On the duplicates screen:
- In each set of copies, the original (the name without `(1)`) is kept and the copies are binned.
- **Bin all extra copies** applies that to every set in one go.
- You can still click any photo to switch it between keep and bin.

## Run it

Requires Python 3.9+.

### Windows (iCloud for Windows)
Double-click `SwipeWipe.bat`, or run `python swipewipe.py`.

- Reads `%USERPROFILE%\Pictures\iCloud Photos\Photos`. Set `SWIPEWIPE_DIR` to use another folder.
- Optional: `pip install pillow` for faster, smaller previews.
- Confirmed deletions go to the Recycle Bin. iCloud syncs them to all your devices, and they stay in **Recently Deleted** for 30 days.

### macOS (Photos app)
1. `python3 -m pip install --user osxphotos pillow` (the launcher also does this for you).
2. Give your terminal **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access), so it can read the Photos library.
3. Double-click `SwipeWipe.command`, or run `python3 swipewipe.py`.
   - If macOS blocks the .command file, run `chmod +x SwipeWipe.command` once, or right-click it and choose Open.

Apple doesn't let scripts delete photos. So on a Mac, "Delete" adds the binned photos to an album called **SwipeWipe - To Delete**. To finish, open that album in Photos and press ⌘A, then ⌘⌫. The first time, macOS asks whether the terminal may control Photos. Allow it.

With "Optimize Mac Storage" turned on, some originals exist only in iCloud. Those photos show Photos' own preview instead, and videos may not play.

## Progress sync between computers

Progress is saved to **iCloud Drive → SwipeWipe/progress.json** whenever iCloud Drive is available. If it isn't, it's saved next to the script. Set `SWIPEWIPE_STATE` to use a different file.

Each photo is matched across computers by its original filename and byte size. So if you swipe a photo on the PC, it shows as reviewed on the Mac too. Every save re-reads the file and merges, so using both machines is fine. Avoid swiping on both at the very same moment.

Progress files and caches are in `.gitignore`. They contain your photo filenames, so keep them out of the repo.

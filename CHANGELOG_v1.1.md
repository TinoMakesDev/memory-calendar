## What's new in v1.1

Highlights:

- **Event export/import JSON** — export and import events (categories) as JSON, plus export media files from an event to a folder.
- **Background thumbnail generation** — opt-in off-screen thumbnail generation with a determinate progress bar (X/Y) in Gallery view and an indeterminate spinner with a text counter in calendar modes.
- **Viewer: orientation & fullscreen** — when navigating between frames with different orientations (portrait/landscape), the image is automatically scaled to fit the screen according to its own aspect ratio.
- **Drag-and-drop from Viewer** — media files can now be dragged from the viewer to Explorer / Finder.
- **EXIF priority fixed** — the EXIF capture date is now correctly used in the priority chain (previously it could be silently ignored).
- **Manual relink of lost files** — Settings → Data safety now offers a manual flow to point at the new folder and reattach lost files back to the calendar.
- **Smarter tab navigation** — moving between Gallery/Viewer and Settings/Manager returns the user exactly where they left off.

**UX improvements:**
- Progress bar visible in dark theme (dedicated colors)
- "Apply Priority and Resort" dialog 480×160, readable numbers
- Gallery scroll position preserved
- Stable background preview progress
- Language change progress dialog


**Cross-platform improvements:**
- Fusion style only on Windows/Linux
- Platform-aware icon (`.icns` / `.png` / `.ico`)
- Linux font detection via `QFontDatabase`
- XDG_DATA_HOME absolute check
- Scanner skips `._*` AppleDouble files
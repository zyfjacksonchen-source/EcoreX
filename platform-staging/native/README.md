# Native platform boundary

The Windows binaries are built only by an x64 MSVC developer environment with
`/Brepro`, Control Flow Guard, CET compatibility, ASLR and NX enabled. The
build script records compiler, linker and output SHA-256 values. The sandbox
host accepts only the fixed `python -I <signed-pack>` child shape, creates a
kill-on-close Job Object, and uses an AppContainer without network capability
for `workspace-write`.

The macOS launcher is a small `execv` bridge to the fixed signed Pack Python.
Workspace enforcement uses the system-owned `/usr/bin/sandbox-exec` Seatbelt
backend and its behavioral probe; missing Seatbelt support fails staging and
keeps shell unavailable.

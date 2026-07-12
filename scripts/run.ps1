#Requires -Version 5.1
$ErrorActionPreference = "Stop"
[Console]::Error.WriteLine("The v0.3 source installer/runtime entrypoint is retired.")
[Console]::Error.WriteLine("Install a signed EcoreX v1 release and launch it through ecorex-bootstrap.")
[Console]::Error.WriteLine("Direct source assembly, git pull, pip install, npm build, and app.py startup are not supported.")
exit 78

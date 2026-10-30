$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'login_qoder.py')
exit $LASTEXITCODE

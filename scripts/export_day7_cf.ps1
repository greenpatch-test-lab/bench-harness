# Export D1 tables (scans & waitlist_reservations) into bench-harness/results/day7_exports/
$expDir = "e:\market research\bench-harness\results\day7_exports"
New-Item -ItemType Directory -Force -Path $expDir | Out-Null

$scansRaw = wrangler d1 execute greenpatch-d1 --remote --json --command="SELECT * FROM scans ORDER BY created_at ASC;"
$scansObj = ($scansRaw | ConvertFrom-Json)[0].results
$scansObj | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 "$expDir\scans.json"

$wlRaw = wrangler d1 execute greenpatch-d1 --remote --json --command="SELECT * FROM waitlist_reservations ORDER BY created_at ASC;"
$wlObj = ($wlRaw | ConvertFrom-Json)[0].results
$wlObj | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 "$expDir\waitlist_reservations.json"

Write-Output ("EXPORTED: scans={0}, waitlist_reservations={1}" -f @($scansObj).Count, @($wlObj).Count)

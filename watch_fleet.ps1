# Live view of the grid fleet, for Windows PowerShell.
#
#   .\watch_fleet.ps1              # every 20s against production
#   .\watch_fleet.ps1 10           # every 10s
#   .\watch_fleet.ps1 10 http://localhost:8000
#
# The bash version (watch_fleet.sh) needs Git Bash or WSL. This one runs in
# stock Windows PowerShell 5.1, which rejects '&&' as a statement separator -
# the exact error that sent this file into existence. Nothing here uses '&&'.
#
# It leads with TOTAL - realized plus unrealized - because that is the only
# figure that can go down. Realized cannot: a grid slice only ever sells ABOVE
# its own entry, so the closed-trade ledger is positive by construction while
# underwater slices sit unclosed. A missing half makes the total unmeasurable
# rather than quietly equal to the half that loaded.

param(
    [int]$IntervalSeconds = 20,
    [string]$BaseUrl = "https://empire-v2-production.up.railway.app"
)

# Windows PowerShell 5.1 still defaults to TLS 1.0 on some boxes; Railway
# refuses that, and the failure looks like a generic connection error.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Url = "$BaseUrl/api/trading-dashboard/live-ops"

function Write-Money {
    param($Value, [string]$Label = "", [switch]$Signed)
    if ($null -eq $Value) {
        Write-Host ("{0}  --  " -f $Label) -ForegroundColor DarkGray -NoNewline
        return
    }
    $n = [double]$Value
    # Sign OUTSIDE the currency symbol. "$-594.99" reads as a typo, and a
    # negative free-cash figure is the one number here nobody can misread.
    $sign = ""
    if ($n -gt 0 -and $Signed) { $sign = "+" }
    if ($n -lt 0) { $sign = "-" }
    $text = "{0}{1}`${2:N2}" -f $Label, $sign, [Math]::Abs($n)
    $color = "Gray"
    if ($n -gt 0) { $color = "Green" }
    if ($n -lt 0) { $color = "Red" }
    Write-Host $text -ForegroundColor $color -NoNewline
}

function Get-Section {
    param($Payload, [string]$Name)
    $s = $Payload.$Name
    if ($null -eq $s) { return $null }
    if (-not $s.ok) { return $null }
    return $s.data
}

while ($true) {
    Clear-Host
    Write-Host ("GRID FLEET  " + (Get-Date -Format "HH:mm:ss")) -ForegroundColor White
    Write-Host ("=" * 62) -ForegroundColor DarkGray

    try {
        $d = Invoke-RestMethod -Uri ($Url + "?t=" + [DateTimeOffset]::Now.ToUnixTimeMilliseconds()) `
                               -TimeoutSec 30 -Headers @{ "Cache-Control" = "no-cache" }
    } catch {
        Write-Host "Cannot reach the server: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "(last good screen was above; retrying)" -ForegroundColor DarkGray
        Start-Sleep -Seconds $IntervalSeconds
        continue
    }

    # ---- can it trade -------------------------------------------------
    $runner = Get-Section $d "runner"
    if ($null -eq $runner) {
        Write-Host "runner: unavailable" -ForegroundColor Red
    } else {
        foreach ($g in $runner.gates) {
            if ($g.ok) {
                Write-Host "  ok      " -ForegroundColor Green -NoNewline
            } else {
                Write-Host "  BLOCKED " -ForegroundColor Red -NoNewline
            }
            Write-Host ("{0}: {1}" -f $g.name, $g.detail)
            if (-not $g.ok) { Write-Host ("          fix: " + $g.fix) -ForegroundColor Yellow }
        }
        $age = $runner.last_activity_age_seconds
        if ($null -eq $age) {
            Write-Host "  bot last wrote to the log: never" -ForegroundColor DarkGray
        } elseif ($age -lt 120) {
            Write-Host ("  bot last wrote to the log: {0:N0}s ago" -f $age) -ForegroundColor DarkGray
        } else {
            Write-Host ("  bot last wrote to the log: {0:N0}m ago" -f ($age / 60)) -ForegroundColor DarkGray
        }
    }

    # ---- the headline: TOTAL ------------------------------------------
    Write-Host ""
    $head = Get-Section $d "headline"
    if ($null -ne $head) {
        Write-Host "TOTAL     " -ForegroundColor White -NoNewline
        if ($head.measurable) {
            Write-Money $head.total_usd -Signed
        } else {
            Write-Host "not measurable" -ForegroundColor Yellow -NoNewline
        }
        Write-Host "   taken + still open" -ForegroundColor DarkGray
        Write-Host "  taken      " -ForegroundColor DarkGray -NoNewline
        Write-Money $head.realized_usd -Signed
        $trips = $head.round_trips
        if ($null -eq $trips) { $trips = 0 }
        Write-Host ("   {0} closed round trip(s)" -f $trips) -ForegroundColor DarkGray
        Write-Host "  still open " -ForegroundColor DarkGray -NoNewline
        Write-Money $head.unrealized_usd -Signed
        Write-Host "   marked at the current price" -ForegroundColor DarkGray
        if ($head.warning) { Write-Host ("  ! " + $head.warning) -ForegroundColor Yellow }
    } else {
        # Older deploy with no headline section - fall back to realized only.
        $tr = Get-Section $d "trades"
        if ($null -ne $tr) {
            Write-Host "REALIZED  " -ForegroundColor White -NoNewline
            Write-Money $tr.total_realized_pnl -Signed
            Write-Host ("   from {0} closed round trip(s)" -f $tr.total_trade_count) -ForegroundColor DarkGray
        }
    }

    # ---- branches ------------------------------------------------------
    Write-Host ""
    $gr = Get-Section $d "grid"
    if ($null -eq $gr) {
        Write-Host "branches: unavailable" -ForegroundColor Red
    } else {
        $bs = @($gr.branches)
        $holding = @($bs | Where-Object { $_.open_slices -gt 0 }).Count
        Write-Host ("BRANCHES  {0} total - {1} holding - " -f $bs.Count, $holding) -NoNewline -ForegroundColor White
        Write-Money $gr.total_allocated_usd "allocated "
        Write-Host "  " -NoNewline
        # Free cash is the number that decides whether anything can fill.
        Write-Money $gr.real_free_cash_usd "free "
        Write-Host ""

        foreach ($b in ($bs | Sort-Object -Property open_slices -Descending)) {
            $slices = $b.open_slices
            if ($null -eq $slices) { $slices = 0 }
            if ($slices -gt 0) {
                Write-Host "  * " -ForegroundColor Cyan -NoNewline
                $state = "{0}/{1} slices" -f $slices, $b.num_levels
            } else {
                Write-Host "  o " -ForegroundColor DarkGray -NoNewline
                $state = "flat, waiting for a dip"
            }
            Write-Host ("{0,-10}" -f $b.product_id) -NoNewline
            Write-Money $b.allocated_usd
            Write-Host ("  {0,-24}" -f $state) -NoNewline
            Write-Money $b.total_unrealized_net_usd -Signed
            $step = $b.grid_pct
            if ($null -ne $step) {
                Write-Host ("  step {0:N2}%" -f ([double]$step * 100)) -ForegroundColor DarkGray
            } else {
                Write-Host ""
            }
        }
    }

    Write-Host ""
    Write-Host ("refreshing every {0}s - Ctrl+C to stop" -f $IntervalSeconds) -ForegroundColor DarkGray
    Start-Sleep -Seconds $IntervalSeconds
}

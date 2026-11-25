# Shelly Gen4 plug (S4PL-00116US) that powers the bench USB hub: both boards, BT600 and Pixel USB.
# BigBoy's Wi-Fi joins the plug's own access point; the plug is at 192.168.33.1. Run this from Windows PowerShell.
# A cycle always uses toggle_after, so the plug turns itself back on even if the caller dies.
# Keep the off time momentary (1-2 s). The relay is mechanical, so run dozens of cycles, not thousands.
param(
  [ValidateSet("cycle","status","on")] [string]$Action = "status",
  [ValidateRange(1,2)] [int]$OffSeconds = 1,
  [string]$PlugHost = "192.168.33.1"
)
$base = "http://$PlugHost/rpc"
switch ($Action) {
  "cycle"  { (Invoke-WebRequest -UseBasicParsing "$base/Switch.Set?id=0&on=false&toggle_after=$OffSeconds").Content }
  "on"     { (Invoke-WebRequest -UseBasicParsing "$base/Switch.Set?id=0&on=true").Content }
  "status" { (Invoke-WebRequest -UseBasicParsing "$base/Switch.GetStatus?id=0").Content }
}

import datetime
import json
import re
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path

import requests
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table

VERBOSE = "--verbose" in sys.argv

LOG_FILE = Path("netlog.jsonl")
sampleCount = 0
lastSampleTime = None
NORMAL_INTERVAL = 60
BOOSTED_INTERVAL = 15
BOOSTED_DURATION = datetime.timedelta(minutes=5)

boostEnd = datetime.datetime.now()
console = Console()


def CheckVpnStatus():
    """
    Return 'ON' if a utun interface is present, else 'OFF'.
    """
    output = subprocess.check_output(["ifconfig"], text=True)
    return "ON" if "utun" in output else "OFF"


def PingTest():
    """
    Ping 8.8.8.8 four times. Return (avg_ms, packet_loss_pct).
    """
    proc = subprocess.run(
        ["ping", "-c", "4", "8.8.8.8"], capture_output=True, text=True
    )
    out = proc.stdout
    lossMatch = re.search(r"(\d+)% packet loss", out)
    packetLoss = float(lossMatch.group(1)) if lossMatch else 0.0
    avgMatch = re.search(r" = [\d\.]+/([\d\.]+)/", out)
    pingMs = float(avgMatch.group(1)) if avgMatch else 0.0
    return pingMs, packetLoss


def SpeedTest():
    """
    Run speedtest-cli in JSON mode. Return (download_mbps, upload_mbps).
    """
    proc = subprocess.run(["speedtest-cli", "--json"], capture_output=True, text=True)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return 0.0, 0.0
    download = data.get("download", 0) / 1e6
    upload = data.get("upload", 0) / 1e6
    return download, upload


def GetWifiSignal():
    """
    On macOS, get RSSI from airport utility. Return dBm or None.
    """
    try:
        proc = subprocess.run(
            [
                "/System/Library/PrivateFrameworks/Apple80211.framework"
                "/Versions/Current/Resources/airport",
                "-I",
            ],
            capture_output=True,
            text=True,
        )
        match = re.search(r"agrCtlRSSI: (-\d+)", proc.stdout)
        return int(match.group(1)) if match else None
    except:
        return None


def TestUrls(extra: bool = False):
    """
    Test a list of sites with 5s timeout. Return list of failed URLs.
    """
    sites = [
        "https://www.google.com",
        "https://www.instagram.com",
        "https://chat.openai.com",
        "https://www.youtube.com",
    ]
    if extra:
        sites.extend(
            [
                "https://www.twitter.com",
                "https://www.reddit.com",
                "https://www.linkedin.com",
            ]
        )
    failed = []
    for url in sites:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code >= 400:
                failed.append(url)
        except:
            failed.append(url)
    return failed


def WriteToLog(entry):
    """
    Append a JSON line to the log file.
    """
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry, indent=4) + "\n")


def RunTrackerLoop():
    """Main loop: collect and log data and display live dashboard."""
    global boostEnd
    global sampleCount
    global lastSampleTime

    placeholder = Table.grid(padding=(0, 1))
    placeholder.add_column(justify="center")
    placeholder.add_row("[bold cyan]Initializing network diagnostics...[/bold cyan]")
    placeholder.add_row("[dim]Waiting for first data sample[/dim]")
    placeholder.add_row("[blue]⌛ Please wait...[/blue]")

    with Live(
        Panel(placeholder, title="Internet Status Monitor"),
        console=console,
        refresh_per_second=4,
    ) as live:
        while True:
            now = datetime.datetime.now()
            elapsedStr = (
                "–" if not lastSampleTime else f"{(now - lastSampleTime).seconds}s"
            )
            lastSampleTime = now
            nowIso = now.isoformat()
            vpnStatus = CheckVpnStatus()
            pingMs, packetLoss = PingTest()
            download, upload = SpeedTest()
            wifiSignal = GetWifiSignal()
            interval = BOOSTED_INTERVAL if now < boostEnd else NORMAL_INTERVAL
            failedSites = TestUrls(extra=(interval == NORMAL_INTERVAL))

            entry = {
                "timestamp": nowIso,
                "vpn_status": vpnStatus,
                "ping_ms": pingMs,
                "packet_loss": packetLoss,
                "download_mbps": download,
                "upload_mbps": upload,
                "wifi_signal_dbm": wifiSignal,
                "failed_sites": failedSites,
            }
            WriteToLog(entry)
            sampleCount += 1
            if VERBOSE:
                console.print(entry)

            table = Table.grid(expand=True)
            table.add_column(justify="right", style="bold magenta", ratio=1)
            table.add_column(justify="left", style="bold", ratio=3)
            table.add_row("Time", f"[white]{now.strftime('%H:%M:%S')}[/white]")
            vpnStr = "[green]ON[/green]" if vpnStatus == "ON" else "[red]OFF[/red]"
            table.add_row("VPN", vpnStr)
            table.add_row("Ping (ms)", f"[cyan]{pingMs:.1f}[/cyan]")
            table.add_row("Loss (%)", f"[yellow]{packetLoss:.1f}[/yellow]")
            table.add_row("Down", f"[blue]{download:.1f} Mbps[/blue]")
            table.add_row("Up", f"[blue]{upload:.1f} Mbps[/blue]")

            if wifiSignal is None:
                signalStr = "N/A"
            elif wifiSignal > -60:
                signalStr = f"[green]{wifiSignal} dBm[/green]"
            elif wifiSignal > -75:
                signalStr = f"[yellow]{wifiSignal} dBm[/yellow]"
            else:
                signalStr = f"[red]{wifiSignal} dBm[/red]"
            table.add_row("Wi-Fi", signalStr)

            if failedSites:
                failsFormatted = "\n".join(
                    f"[red]- {url.replace('https://', '')}[/red]" for url in failedSites
                )
            else:
                failsFormatted = "[green]None[/green]"
            table.add_row("Fails", failsFormatted)

            countdown = interval
            while countdown:
                title = (
                    f"[b bright_white]Internet Status Monitor[/b bright_white]  "
                    f"[dim](next in {countdown}s, +{elapsedStr})[/dim]  "
                    f"[green]#{sampleCount}[/green]"
                )
                live.update(Panel(table, title=title))
                time.sleep(1)
                countdown -= 1


def ManualMarkerLoop():
    """
    Listen for 'm' key. On press, log marker and trigger boosted mode.
    """
    global boostEnd
    fd = sys.stdin.fileno()
    origSettings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch == "m":
                nowIso = datetime.datetime.now().isoformat()
                boostEnd = datetime.datetime.now() + BOOSTED_DURATION
                WriteToLog({"timestamp": nowIso, "marker": "MANUAL_MARK"})
                console.print(
                    f"[{nowIso}] [bold yellow]Manual marker logged.[/bold yellow]"
                )
            time.sleep(0.1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, origSettings)


def main():
    threading.Thread(target=ManualMarkerLoop, daemon=True).start()
    RunTrackerLoop()


if __name__ == "__main__":
    main()

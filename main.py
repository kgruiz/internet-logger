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
    """
    Main loop: collect and log data and display live dashboard.
    """
    global boostEnd
    global sampleCount
    placeholderTable = Table(show_header=True, header_style="bold magenta")
    placeholderTable.add_column("Metric")
    placeholderTable.add_column("Value")
    for label in [
        "Time",
        "VPN",
        "Ping (ms)",
        "Loss (%)",
        "Down",
        "Up",
        "Wi-Fi",
        "Fails",
        "Samples",
    ]:
        placeholderTable.add_row(label, "N/A")

    with Live(
        Panel(placeholderTable, title="Internet Status Monitor  •  loading..."),
        console=console,
        refresh_per_second=4,
    ) as live:
        while True:
            now = datetime.datetime.now()
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

            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Metric")
            table.add_column("Value")
            table.add_row("Time", now.strftime("%H:%M:%S"))
            table.add_row("VPN", vpnStatus)
            table.add_row("Ping (ms)", f"{pingMs:.1f}")
            table.add_row("Loss (%)", f"{packetLoss:.1f}")
            table.add_row("Down", f"{download:.1f} Mbps")
            table.add_row("Up", f"{upload:.1f} Mbps")
            if wifiSignal:
                if wifiSignal > -60:
                    signalStr = f"[green]{wifiSignal} dBm[/green]"
                elif wifiSignal > -75:
                    signalStr = f"[yellow]{wifiSignal} dBm[/yellow]"
                else:
                    signalStr = f"[red]{wifiSignal} dBm[/red]"
            else:
                signalStr = "N/A"
            table.add_row("Wi-Fi", signalStr)
            failMsg = ", ".join(failedSites) if failedSites else "None"
            failColor = "bold red" if failedSites else "green"
            table.add_row("Fails", f"[{failColor}]{failMsg}[/{failColor}]")
            table.add_row("Samples", str(sampleCount))

            countdown = interval

            while countdown:
                live.update(
                    Panel(
                        table,
                        title=f"Internet Status Monitor  •  next check in {countdown}s",
                    )
                )
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

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
NORMAL_INTERVAL_SEC = 60
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
    except Exception:
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
        except Exception:
            failed.append(url)
    return failed


def WriteToLog(entry):
    """Append a JSON record to the log file."""
    with LOG_FILE.open("a") as logFile:
        json.dump(entry, logFile, indent=4)
        logFile.write("\n")


def BuildTable(
    vpnStatus=None,
    pingMs=None,
    packetLoss=None,
    download=None,
    upload=None,
    wifiSignal=None,
    fails=None,
    sinceLast=0,
    nextIn=0,
):
    table = Table(show_header=False, pad_edge=False)
    table.add_column(style="white", justify="left")
    table.add_column(justify="right")

    table.add_row("Time", datetime.datetime.now().strftime("%H:%M:%S"))
    if vpnStatus is None:
        table.add_row("VPN", "N/A")
    else:
        vpnStr = "[green]ON[/green]" if vpnStatus == "ON" else "[red]OFF[/red]"
        table.add_row("VPN", vpnStr)

    pingStr = "N/A" if pingMs is None else f"[cyan]{pingMs:.1f}[/cyan]"
    table.add_row("Ping (ms)", pingStr)

    lossStr = "N/A" if packetLoss is None else f"[yellow]{packetLoss:.1f}[/yellow]"
    table.add_row("Loss (%)", lossStr)

    downStr = "N/A" if download is None else f"[blue]{download:.1f} Mbps[/blue]"
    table.add_row("Down", downStr)

    upStr = "N/A" if upload is None else f"[blue]{upload:.1f} Mbps[/blue]"
    table.add_row("Up", upStr)

    if wifiSignal is None:
        wifiStr = "N/A"
    elif wifiSignal > -60:
        wifiStr = f"[green]{wifiSignal} dBm[/green]"
    elif wifiSignal > -75:
        wifiStr = f"[yellow]{wifiSignal} dBm[/yellow]"
    else:
        wifiStr = f"[red]{wifiSignal} dBm[/red]"
    table.add_row("Wi-Fi:", wifiStr)

    failsGrid = Table.grid(padding=0)
    if fails:
        for url in fails:
            failsGrid.add_row(f"[red]{url.replace('https://', '')}[/red]")
    else:
        failsGrid.add_row("[green]None[/green]")
    table.add_row("Fails", failsGrid)

    table.add_row("Samples taken", f"[green]{sampleCount}[/green]")
    table.add_row("Since last", f"{sinceLast}s")
    table.add_row("Next in", f"{nextIn}s")

    return table


def RunTrackerLoop():
    """Main loop: collect and log data and display live dashboard."""
    global boostEnd
    global sampleCount
    global lastSampleTime

    with Live(
        Panel(Spinner("dots"), title="Internet Status Monitor"),
        console=console,
        refresh_per_second=4,
    ) as live:
        table = BuildTable()
        live.update(Panel(table, title="Internet Status Monitor"))
        nextSampleTime = datetime.datetime.now()

        while True:
            live.update(
                Panel(
                    Spinner("dots", text="Collecting sample..."),
                    title="Internet Status Monitor",
                )
            )

            now = datetime.datetime.now()
            timeSinceLast = (
                0
                if lastSampleTime is None
                else int((now - lastSampleTime).total_seconds())
            )
            lastSampleTime = now
            nowIso = now.isoformat()

            vpnStatus = CheckVpnStatus()
            pingMs, packetLoss = PingTest()
            download, upload = SpeedTest()
            wifiSignal = GetWifiSignal()
            interval = BOOSTED_INTERVAL if now < boostEnd else NORMAL_INTERVAL_SEC
            failedSites = TestUrls(extra=(interval == NORMAL_INTERVAL_SEC))

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

            nextSampleTime = now + datetime.timedelta(seconds=interval)
            table = BuildTable(
                vpnStatus,
                pingMs,
                packetLoss,
                download,
                upload,
                wifiSignal,
                failedSites,
                timeSinceLast,
                int((nextSampleTime - now).total_seconds()),
            )

            while True:
                now = datetime.datetime.now()
                timeUntilNext = int((nextSampleTime - now).total_seconds())
                if timeUntilNext < 0:
                    break
                table = BuildTable(
                    vpnStatus,
                    pingMs,
                    packetLoss,
                    download,
                    upload,
                    wifiSignal,
                    failedSites,
                    int((now - lastSampleTime).total_seconds()),
                    timeUntilNext,
                )
                live.update(
                    Panel(
                        table,
                        title="Internet Status Monitor",
                        subtitle=f"(next in {timeUntilNext}s)",
                    )
                )
                time.sleep(1)


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

import datetime
import json
import re
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import deque
from pathlib import Path

import requests
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

# Constants
LOG_FILE_PATH = Path("netlog.jsonl")
NORMAL_INTERVAL_SEC = 60
BOOSTED_INTERVAL_SEC = 15
BOOSTED_DURATION = datetime.timedelta(minutes=5)

# Global state
console = Console()
boostEndTime = datetime.datetime.now()
sampleCount = 0
lastSampleTime = None
durationList = []
marksDeque = deque(maxlen=5)
totalMarksCount = 0


def CheckVpnStatus():
    output = subprocess.check_output(["ifconfig"], text=True)
    return "ON" if "utun" in output else "OFF"


def PingTest():
    proc = subprocess.run(
        ["ping", "-c", "4", "8.8.8.8"], capture_output=True, text=True
    )
    lossMatch = re.search(r"(\d+)% packet loss", proc.stdout)
    avgMatch = re.search(r" = [\d\.]+/([\d\.]+)/", proc.stdout)
    packetLoss = float(lossMatch.group(1)) if lossMatch else 0.0
    pingMs = float(avgMatch.group(1)) if avgMatch else 0.0
    return pingMs, packetLoss


def SpeedTest():
    proc = subprocess.run(["speedtest-cli", "--json"], capture_output=True, text=True)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return 0.0, 0.0
    download = data.get("download", 0) / 1e6
    upload = data.get("upload", 0) / 1e6
    return download, upload


def GetWifiSignal():
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


def TestUrls(extra=False):
    baseSites = [
        "https://www.google.com",
        "https://chat.openai.com",
        "https://www.youtube.com",
    ]
    extraSites = ["https://www.twitter.com", "https://www.reddit.com"]
    sites = baseSites + (extraSites if extra else [])
    failed = []
    for url in sites:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code >= 400:
                failed.append(url.replace("https://", ""))
        except Exception:
            failed.append(url.replace("https://", ""))
    return failed


def WriteToLog(entry):
    with LOG_FILE_PATH.open("a") as f:
        json.dump(entry, f, indent=4)
        f.write("\n")


def RenderDashboard(
    vpnStatus: str,
    boosted: bool,
    currentTime: str,
    samplesTaken: int,
    timeSince: int,
    timeUntil: int,
    avgDuration: int,
    sampleStart: str,
    sampleEnd: str,
    lastDuration: int,
    pingMs: float,
    packetLoss: float,
    download: float,
    upload: float,
    wifiSignal: int | None,
    failedSites: list[str],
    recentMarks: list[str],
    totalMarks: int,
    indicator: str = "",
    indicatorMs: int = 0,
):
    layout = Layout()
    layout.split_column(
        Layout(name="status", size=14),
        Layout(name="sample", size=18),
        Layout(name="marks", ratio=1),
    )

    # Status panel
    statusGrid = Table.grid(expand=True)
    statusGrid.add_column(ratio=3)
    statusGrid.add_column(ratio=2)
    statusGrid.add_column(ratio=2)

    metrics = Table(
        box=box.SQUARE,
        show_header=False,
        pad_edge=True,
        expand=True,
        border_style="cyan",
    )
    metrics.add_column(justify="left")
    metrics.add_column(justify="right")
    metrics.add_row("Time", f"[cyan]{currentTime}[/cyan]")
    metrics.add_row("Samples", f"[green]{samplesTaken}[/green]")
    metrics.add_row("Since", f"[yellow]{timeSince}s[/yellow]")
    metrics.add_row("Until", f"[yellow]{timeUntil}s[/yellow]")
    metrics.add_row("AvgDur", f"[cyan]{avgDuration}ms[/cyan]")
    metrics.add_row("", "")
    metrics.add_row("Marks", f"[green]{totalMarks}[/green]")

    indicatorPanel = Panel(
        Spinner("dots", text=f"{indicator} {indicatorMs}ms", style="magenta"),
        border_style="magenta",
        padding=(0, 1),
    ) if indicator else Panel("", border_style="magenta")

    vpnBox = Table(
        box=box.SQUARE,
        show_header=False,
        pad_edge=True,
        expand=True,
        border_style="cyan",
    )
    vpnBox.add_column()
    vpnBox.add_row(
        "VPN: [green]ON[/green]" if vpnStatus == "ON" else "VPN: [red]OFF[/red]"
    )
    vpnBox.add_row(
        "Boosted: [yellow]✅[/yellow]" if boosted else "Boosted: [red]❌[/red]"
    )
    if boosted:
        remaining = int((boostEndTime - datetime.datetime.now()).total_seconds())
        vpnBox.add_row("Ends in", f"[magenta]{remaining}s[/magenta]")

    statusGrid.add_row(
        Panel(metrics, border_style="cyan", padding=(0, 1)),
        indicatorPanel,
        Panel(vpnBox, border_style="cyan", padding=(0, 1)),
    )
    layout["status"].update(
        Panel(
            statusGrid,
            title="[bold cyan]Status Summary[/bold cyan]",
            border_style="cyan",
        )
    )

    # Sample panel
    sampleTable = Table(box=box.SQUARE, show_header=False, pad_edge=True, expand=True)
    sampleTable.add_column(justify="left")
    sampleTable.add_column(justify="right")
    sampleTable.add_row("Start", f"[cyan]{sampleStart}[/cyan]")
    sampleTable.add_row("End", f"[cyan]{sampleEnd}[/cyan]")
    sampleTable.add_row("Dur", f"[green]{lastDuration}ms[/green]")
    pingText = f"[cyan]{pingMs:.1f}[/cyan]" if pingMs > 0 else "[dim]—[/dim]"
    sampleTable.add_row("Ping", pingText)
    if packetLoss > 1:
        lossColor = "red"
    elif packetLoss > 0:
        lossColor = "yellow"
    else:
        lossColor = "green"
    lossText = f"[{lossColor}]{packetLoss:.1f}[/{lossColor}]" if packetLoss > 0 else "[dim]0[/dim]"
    sampleTable.add_row("Loss", lossText)
    downText = f"[blue]{download:.1f} Mbps[/blue]" if download > 0 else "[dim]—[/dim]"
    upText = f"[blue]{upload:.1f} Mbps[/blue]" if upload > 0 else "[dim]—[/dim]"
    sampleTable.add_row("Down", downText)
    sampleTable.add_row("Up", upText)
    if wifiSignal is None:
        wifiText = "[dim]—[/dim]"
    elif wifiSignal > -60:
        wifiText = f"[green]{wifiSignal} dBm[/green]"
    elif wifiSignal > -75:
        wifiText = f"[yellow]{wifiSignal} dBm[/yellow]"
    else:
        wifiText = f"[red]{wifiSignal} dBm[/red]"
    sampleTable.add_row("Wi-Fi", wifiText)
    failsGrid = Table.grid(padding=0)
    if failedSites:
        for site in failedSites:
            failsGrid.add_row(Text(site, style="yellow", overflow="ellipsis"))
    else:
        failsGrid.add_row("[green]—[/green]")
    sampleTable.add_row("Fail", failsGrid)

    layout["sample"].update(
        Panel(
            sampleTable,
            title="[bold green]Latest Sample[/bold green]",
            border_style="green",
        )
    )

    # Marks panel
    marksTable = Table(box=box.SQUARE, show_header=False, pad_edge=True, expand=True)
    marksTable.add_column()
    if recentMarks:
        for mark in recentMarks:
            marksTable.add_row(f"[magenta]{mark}[/magenta]")
    else:
        marksTable.add_row("[dim]No marks yet[/dim]")
    overflow = totalMarks - len(recentMarks)
    if overflow > 0:
        marksTable.add_row(f"[yellow]... ({overflow} more omitted)[/yellow]")

    layout["marks"].update(
        Panel(
            marksTable,
            title="[bold yellow]Recent Marks[/bold yellow]",
            border_style="yellow",
        )
    )

    return layout


def RunTrackerLoop():
    global boostEndTime, sampleCount, lastSampleTime

    with Live(console=console, refresh_per_second=4) as live:
        # initial render
        nowStr = datetime.datetime.now().strftime("%H:%M:%S")
        live.update(
            RenderDashboard(
                vpnStatus="OFF",
                boosted=False,
                currentTime=nowStr,
                samplesTaken=sampleCount,
                timeSince=0,
                timeUntil=NORMAL_INTERVAL_SEC,
                avgDuration=0,
                sampleStart=nowStr,
                sampleEnd=nowStr,
                lastDuration=0,
                pingMs=0.0,
                packetLoss=0.0,
                download=0.0,
                upload=0.0,
                wifiSignal=None,
                failedSites=[],
                recentMarks=list(marksDeque),
                totalMarks=totalMarksCount,
                indicator="Initializing…",
                indicatorMs=0,
            )
        )

        # sampling loop
        while True:
            startTime = time.perf_counter()
            startDt = datetime.datetime.now()

            stopEvent = threading.Event()

            def _indicator_loop():
                while not stopEvent.is_set():
                    nowDt = datetime.datetime.now()
                    elapsedMs = int((time.perf_counter() - startTime) * 1000)
                    live.update(
                        RenderDashboard(
                            vpnStatus=CheckVpnStatus(),
                            boosted=(nowDt < boostEndTime),
                            currentTime=nowDt.strftime("%H:%M:%S"),
                            samplesTaken=sampleCount,
                            timeSince=int(
                                (startDt - (lastSampleTime or startDt)).total_seconds()
                            ),
                            timeUntil=0,
                            avgDuration=(
                                int(sum(durationList) / len(durationList))
                                if durationList
                                else 0
                            ),
                            sampleStart=startDt.strftime("%H:%M:%S"),
                            sampleEnd=startDt.strftime("%H:%M:%S"),
                            lastDuration=0,
                            pingMs=0.0,
                            packetLoss=0.0,
                            download=0.0,
                            upload=0.0,
                            wifiSignal=None,
                            failedSites=[],
                            recentMarks=list(marksDeque),
                            totalMarks=totalMarksCount,
                            indicator="Sampling…",
                            indicatorMs=elapsedMs,
                        )
                    )
                    time.sleep(0.1)

            indicatorThread = threading.Thread(target=_indicator_loop, daemon=True)
            indicatorThread.start()

            # perform measurements
            vpn = CheckVpnStatus()
            pingMs, packetLoss = PingTest()
            download, upload = SpeedTest()
            wifi = GetWifiSignal()
            failed = TestUrls(extra=False)

            stopEvent.set()
            indicatorThread.join()

            endDt = datetime.datetime.now()
            durMs = int((time.perf_counter() - startTime) * 1000)
            durationList.append(durMs)
            sampleCount += 1
            lastSampleTime = startDt
            avgMs = int(sum(durationList) / len(durationList))

            entry = {
                "timestamp": startDt.isoformat(),
                "vpn_status": vpn,
                "ping_ms": pingMs,
                "packet_loss": packetLoss,
                "download_mbps": download,
                "upload_mbps": upload,
                "wifi_signal_dbm": wifi,
                "failed_sites": failed,
            }
            WriteToLog(entry)

            # countdown until next
            nextInterval = (
                BOOSTED_INTERVAL_SEC
                if datetime.datetime.now() < boostEndTime
                else NORMAL_INTERVAL_SEC
            )
            while True:
                nowDt = datetime.datetime.now()
                sinceSec = int((nowDt - lastSampleTime).total_seconds())
                untilSec = int(
                    (
                        lastSampleTime
                        + datetime.timedelta(seconds=nextInterval)
                        - nowDt
                    ).total_seconds()
                )
                if untilSec <= 0:
                    break
                nowStr = nowDt.strftime("%H:%M:%S")
                live.update(
                    RenderDashboard(
                        vpnStatus=vpn,
                        boosted=(nowDt < boostEndTime),
                        currentTime=nowStr,
                        samplesTaken=sampleCount,
                        timeSince=sinceSec,
                        timeUntil=untilSec,
                        avgDuration=avgMs,
                        sampleStart=startDt.strftime("%H:%M:%S"),
                        sampleEnd=endDt.strftime("%H:%M:%S"),
                        lastDuration=durMs,
                        pingMs=pingMs,
                        packetLoss=packetLoss,
                        download=download,
                        upload=upload,
                        wifiSignal=wifi,
                        failedSites=failed,
                        recentMarks=list(marksDeque),
                        totalMarks=totalMarksCount,
                        indicator="",
                        indicatorMs=0,
                    )
                )
                time.sleep(1)


def ManualMarkerLoop():
    global boostEndTime, totalMarksCount

    fd = sys.stdin.fileno()
    origSettings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch == "m":
                nowDt = datetime.datetime.now()
                ts = nowDt.strftime("%H:%M:%S")
                boostEndTime = nowDt + BOOSTED_DURATION
                marksDeque.append(ts)
                totalMarksCount += 1
                WriteToLog({"timestamp": nowDt.isoformat(), "marker": "MANUAL_MARK"})
                console.print(f"[{ts}] Manual marker logged.")
            time.sleep(0.1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, origSettings)


def main():
    threading.Thread(target=ManualMarkerLoop, daemon=True).start()
    RunTrackerLoop()


if __name__ == "__main__":
    main()

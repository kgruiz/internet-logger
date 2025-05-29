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
from typing import Deque, Tuple
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
marksDeque: Deque[Tuple[str, str]] = deque(maxlen=5)
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
    recentMarks: list[tuple[str, str]],
    totalMarks: int,
    indicator: str = "",
    indicatorMs: int = 0,
):
    layout = Layout()
    layout.split_column(
        Layout(name="status", size=3),
        Layout(name="metrics", size=4),
        Layout(name="sample", size=8),
        Layout(name="marks", ratio=1),
        Layout(name="hint", size=1),
    )

    # Status panel
    statusGrid = Table.grid(expand=True)
    statusGrid.add_column(justify="center")
    statusGrid.add_column(justify="center")
    statusGrid.add_column(justify="center")

    if indicator:
        statusSpinner = Spinner("dots", text=f"Sampling… {indicatorMs}ms", style="magenta")
    else:
        statusSpinner = Text("Idle", style="dim")

    vpnText = Text("VPN ON", style="green") if vpnStatus == "ON" else Text("VPN OFF", style="red")

    if boosted:
        remaining = int((boostEndTime - datetime.datetime.now()).total_seconds())
        boostText = Text(f"Boosted {remaining}s", style="yellow")
    else:
        boostText = Text("Boosted off", style="dim")

    statusGrid.add_row(statusSpinner, vpnText, boostText)
    layout["status"].update(
        Panel(statusGrid, title="[bold cyan]Status Summary[/bold cyan]", border_style="cyan")
    )

    # Metrics panel
    metrics = Table.grid(expand=True)
    for _ in range(3):
        metrics.add_column(justify="left")
        metrics.add_column(justify="right")
    metrics.add_row(
        "Time",
        f"[cyan]{currentTime}[/cyan]",
        "Samples",
        f"[green]{samplesTaken}[/green]",
        "Marks",
        f"[green]{totalMarks}[/green]",
    )
    metrics.add_row(
        "Since",
        f"[yellow]{timeSince}s[/yellow]",
        "Until",
        f"[yellow]{timeUntil}s[/yellow]",
        "AvgDur",
        f"[cyan]{avgDuration}ms[/cyan]",
    )
    layout["metrics"].update(
        Panel(metrics, title="[bold cyan]Metrics[/bold cyan]", border_style="cyan")
    )

    # Sample panel
    sampleTable = Table.grid(expand=True)
    for _ in range(5):
        sampleTable.add_column()

    sampleTable.add_row(
        f"Start: {sampleStart}",
        f"End: {sampleEnd}",
        f"Dur: {lastDuration}ms",
        "",
        "",
    )

    pingText = f"{pingMs:.1f}ms" if pingMs > 0 else "—"
    if packetLoss > 1:
        lossColor = "red"
    elif packetLoss > 0:
        lossColor = "yellow"
    else:
        lossColor = "green"
    lossText = f"[{lossColor}]{packetLoss:.1f}%[/{lossColor}]"
    downText = f"{download:.1f} Mbps" if download > 0 else "—"
    upText = f"{upload:.1f} Mbps" if upload > 0 else "—"
    if wifiSignal is None:
        wifiText = "—"
    else:
        wifiText = f"{wifiSignal} dBm"

    sampleTable.add_row(
        f"Ping: {pingText}",
        f"Loss: {lossText}",
        f"Down: {downText}",
        f"Up: {upText}",
        f"Signal: {wifiText}",
    )

    fails = ", ".join(failedSites) if failedSites else "None"
    sampleTable.add_row(f"Fails: {fails}", "", "", "", "")

    layout["sample"].update(
        Panel(sampleTable, title="[bold green]Latest Sample[/bold green]", border_style="green")
    )

    # Marks panel
    marksTable = Table(box=box.SQUARE, show_header=True, pad_edge=True, expand=True)
    marksTable.add_column("Time")
    marksTable.add_column("Note")
    if recentMarks:
        for ts, note in recentMarks:
            marksTable.add_row(ts, note)
    else:
        marksTable.add_row("-", "No marks yet")
    overflow = totalMarks - len(recentMarks)
    if overflow > 0:
        marksTable.add_row("", f"... ({overflow} more omitted)")

    layout["marks"].update(
        Panel(
            marksTable,
            title="[bold yellow]Recent Marks[/bold yellow]",
            border_style="yellow",
        )
    )

    layout["hint"].update(Text("Press 'm' to add mark, 'q' to quit", justify="center", style="dim"))

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
            if ch == "q":
                sys.exit(0)
            if ch == "m":
                nowDt = datetime.datetime.now()
                ts = nowDt.strftime("%H:%M:%S")
                boostEndTime = nowDt + BOOSTED_DURATION
                marksDeque.append((ts, "Manual mark"))
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

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
from typing import Deque, Tuple

import requests
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
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

    if samplesTaken == 0:
        totalSamplesStr = str(samplesTaken)
        totalMarksStr = str(totalMarks)
        timeSinceStr = Text("-", justify="center")
        timeUntilStr = Text("-", justify="center")
        avgDurationStr = Text("-", justify="center")
        sampleStartStr = Text("-", justify="center")
        sampleEndStr = Text("-", justify="center")
        lastDurationStr = Text("-", justify="center")
        pingMsStr = Text("-", justify="center")
        packetLossStr = Text("-", justify="center")
        downloadStr = Text("-", justify="center")
        uploadStr = Text("-", justify="center")
        wifiSignalStr = Text("-", justify="center")
        failedSitesStr = Text("-", justify="center")
    else:
        totalSamplesStr = str(samplesTaken)
        totalMarksStr = str(totalMarks)
        timeSinceStr = f"{timeSince}s"
        timeUntilStr = f"{timeUntil}s"
        avgDurationStr = f"{avgDuration}ms"
        sampleStartStr = sampleStart
        sampleEndStr = sampleEnd
        lastDurationStr = f"{lastDuration}ms"
        pingMsStr = f"{pingMs:.1f}"
        packetLossStr = f"{packetLoss:.1f}"
        downloadStr = f"{download:.1f}"
        uploadStr = f"{upload:.1f}"
        wifiSignalStr = f"{wifiSignal} dBm" if wifiSignal is not None else "-"
        failedSitesStr = ", ".join(failedSites) or "-"
    layout.split_column(
        Layout(name="status", size=5),
        Layout(name="metrics", size=8),
        Layout(name="sample", size=13),
        Layout(name="marks", size=11),
        Layout(name="hint", size=1),
    )

    # Status panel
    statusGrid = Table(box=box.SQUARE, expand=True, show_header=False, show_lines=True)
    statusGrid.add_column(justify="center", width=18)
    statusGrid.add_column(justify="center")
    statusGrid.add_column(justify="center")

    statusText = (
        Text(f"{indicator} {indicatorMs}ms", style="magenta")
        if indicator
        else Text("", style="")
    )
    vpnText = (
        Text("VPN ON", style="green")
        if vpnStatus == "ON"
        else Text("VPN OFF", style="red")
    )
    boostText = (
        Text(f"✅ Ends in {timeUntil}s", style="yellow")
        if boosted
        else Text("Boosted off", style="dim")
    )

    statusGrid.add_row(statusText, vpnText, boostText)
    layout["status"].update(
        Panel(
            statusGrid,
            title="[bold yellow] Status Summary [/]",
            box=box.HEAVY,
        )
    )

    # Metrics panel
    metrics = Table(
        box=box.SQUARE, expand=True, show_header=False, show_lines=True, padding=(0, 1)
    )
    metrics.add_column(justify="left", style="bold cyan")
    metrics.add_column(justify="right", style="dim")
    metrics.add_column(justify="left", style="bold cyan")
    metrics.add_column(justify="right", style="dim")
    metrics.add_column(justify="left", style="bold cyan")
    metrics.add_column(justify="right", style="dim")
    metrics.add_row(
        "Current Time",
        currentTime,
        "Total Samples",
        totalSamplesStr,
        "Total Marks",
        totalMarksStr,
    )
    metrics.add_row(
        "Time Since Last",
        timeSinceStr,
        "Time Until Next",
        timeUntilStr,
        "Average Duration",
        avgDurationStr,
    )
    layout["metrics"].update(
        Panel(metrics, title="[bold cyan] Metrics [/]", box=box.HEAVY)
    )

    # Latest Sample panel
    sampleGrid = Table(
        box=box.SQUARE, expand=True, show_header=False, show_lines=True, padding=(0, 1)
    )
    sampleGrid.add_column(justify="left", style="bold green")
    sampleGrid.add_column(justify="right", style="dim")
    sampleGrid.add_column(justify="left", style="bold green")
    sampleGrid.add_column(justify="right", style="dim")
    sampleGrid.add_column(justify="left", style="bold green")
    sampleGrid.add_column(justify="right", style="dim")
    sampleGrid.add_row(
        "Start Time",
        sampleStartStr,
        "End Time",
        sampleEndStr,
        "Duration",
        lastDurationStr,
    )
    sampleGrid.add_row(
        "Ping (ms)",
        pingMsStr,
        "Packet Loss (%)",
        packetLossStr,
        "Download (Mbps)",
        downloadStr,
    )
    sampleGrid.add_row(
        "Upload (Mbps)",
        uploadStr,
        "Signal (dBm)",
        wifiSignalStr,
        "Failed Sites",
        failedSitesStr,
    )
    layout["sample"].update(
        Panel(sampleGrid, title="[bold green] Latest Sample [/]", box=box.HEAVY)
    )

    # Marks panel
    marksTable = Table(
        show_header=True,
        box=box.SQUARE,
        expand=True,
        pad_edge=True,
        show_lines=True,
    )
    marksTable.add_column("Time", no_wrap=True, width=10)
    marksTable.add_column("Note", ratio=1)
    if recentMarks:
        for ts, note in recentMarks:
            marksTable.add_row(ts, note)
        if len(recentMarks) < totalMarks:
            overflow = totalMarks - len(recentMarks)
            marksTable.add_row("", f"... ({overflow} more omitted)")
    else:
        marksTable.add_row("", "[dim italic]No marks yet[/]")
    layout["marks"].update(
        Panel(marksTable, title="[bold magenta] Recent Marks [/]", box=box.HEAVY)
    )

    # Hint line
    layout["hint"].update(
        Text("Press 'm' to add mark | 'q' to quit", justify="center", style="dim")
    )

    return layout


def RunTrackerLoop():
    global boostEndTime, sampleCount, lastSampleTime

    with Live(console=console, refresh_per_second=4, screen=True) as live:
        while True:
            startTime = time.perf_counter()
            startDt = datetime.datetime.now()

            # Sampling indicator thread
            stopEvent = threading.Event()

            def indicator_loop():
                while not stopEvent.is_set():
                    elapsedMs = int((time.perf_counter() - startTime) * 1000)
                    live.update(
                        RenderDashboard(
                            vpnStatus=CheckVpnStatus(),
                            boosted=(datetime.datetime.now() < boostEndTime),
                            currentTime=datetime.datetime.now().strftime("%H:%M:%S"),
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

            indicatorThread = threading.Thread(target=indicator_loop, daemon=True)
            indicatorThread.start()

            # Perform tests
            vpn = CheckVpnStatus()
            pingMs, packetLoss = PingTest()
            download, upload = SpeedTest()
            wifi = GetWifiSignal()
            failed = TestUrls()

            # Stop indicator and record results
            stopEvent.set()
            indicatorThread.join()
            endDt = datetime.datetime.now()
            durMs = int((time.perf_counter() - startTime) * 1000)
            durationList.append(durMs)
            sampleCount += 1
            lastSampleTime = startDt
            avgMs = int(sum(durationList) / len(durationList))

            # Write log entry
            WriteToLog(
                {
                    "timestamp": startDt.isoformat(),
                    "vpn_status": vpn,
                    "ping_ms": pingMs,
                    "packet_loss": packetLoss,
                    "download_mbps": download,
                    "upload_mbps": upload,
                    "wifi_signal_dbm": wifi,
                    "failed_sites": failed,
                }
            )

            # Countdown until next sample
            interval = (
                BOOSTED_INTERVAL_SEC
                if datetime.datetime.now() < boostEndTime
                else NORMAL_INTERVAL_SEC
            )
            while True:
                nowDt = datetime.datetime.now()
                sinceSec = int((nowDt - lastSampleTime).total_seconds())
                untilSec = int(
                    (
                        lastSampleTime + datetime.timedelta(seconds=interval) - nowDt
                    ).total_seconds()
                )
                if untilSec <= 0:
                    break
                live.update(
                    RenderDashboard(
                        vpnStatus=vpn,
                        boosted=(nowDt < boostEndTime),
                        currentTime=nowDt.strftime("%H:%M:%S"),
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

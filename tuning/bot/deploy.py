"""Push replay files to the device over SSH.

Mirrors what `_deploy.py` does for the main game, but only for the
`replays/` folder. Called after every bot run so the latest playable
replays are always available on-device.

If paramiko isn't installed or the device is unreachable, log once and
move on — the local replays are still valid, only the device sync is lost.
"""

from pathlib import Path


HOST = "192.168.1.210"
USER = "root"
PW = "root"
REMOTE_REPLAY_DIR = "/mnt/mmc/Roms/APPS/Pewpew/replays"


def deploy_replays(local_paths, host=HOST, user=USER, pw=PW):
    """Push the given local replay files to the device's replays/ dir."""
    try:
        import paramiko
    except ImportError:
        print("[deploy] paramiko not installed — skipping replay push")
        return False
    try:
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(host, port=22, username=user, password=pw,
                    timeout=8, allow_agent=False, look_for_keys=False)
    except Exception as e:
        print(f"[deploy] device unreachable ({type(e).__name__}: {e}) — skipping")
        return False
    try:
        sftp = cli.open_sftp()
        # mkdir -p the replays folder
        parts = REMOTE_REPLAY_DIR.strip("/").split("/")
        cur = ""
        for p in parts:
            cur = cur + "/" + p
            try:
                sftp.stat(cur)
            except FileNotFoundError:
                sftp.mkdir(cur)
        for src in local_paths:
            src = Path(src)
            dst = f"{REMOTE_REPLAY_DIR}/{src.name}"
            sftp.put(str(src), dst)
            print(f"[deploy] {src.name} -> {dst} ({src.stat().st_size} bytes)")
        # latest.json sits next to the replays.
        local_latest = src.parent / "latest.json"
        if local_latest.is_file():
            sftp.put(str(local_latest),
                     f"{REMOTE_REPLAY_DIR}/latest.json")
            print(f"[deploy] latest.json -> {REMOTE_REPLAY_DIR}/latest.json")
        sftp.close()
    finally:
        cli.close()
    return True

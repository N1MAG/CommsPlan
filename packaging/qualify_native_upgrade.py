"""CI-only disposable native upgrade/backup/rollback qualification for CommsPlan.

Never targets an operator profile. Linux/Windows system package changes require
GitHub Actions; macOS uses an isolated Applications directory in a new workspace.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT/"packaging") not in sys.path: sys.path.insert(0,str(ROOT/"packaging"))
from verify_release_inputs import validated_version
HISTORICAL_COMMIT = "3943d234d972496633c208900a361b52ff7a0f49"
PINS = {
    "windows": ("FreqInOut-2.0.4-windows-x86_64-setup-unsigned.exe", "5444fbbc65ee7a96cb1be7e7013ad9069b94c02975c6d5ad1202c7d75c7ee060", 599820856),
    "linux": ("FreqInOut-2.0.4-linux-amd64.deb", "96bafc48b96b8cf167c36b95342087775621b1a1ba811517d43f3b76b2ab112a", 599820861),
    "macos-arm64": ("FreqInOut-2.0.4-macos-arm64-unsigned.dmg", "db8323674ac5bc6f5dd03aa1a520d7f996656fce1991052fce850bb15dfe98ef", 599820862),
    "macos-x86_64": ("FreqInOut-2.0.4-macos-x86_64-unsigned.dmg", "0bec5c05c58588d4ca55745fba16b9b99f4d87fd0946bdec06c862d1918ff955", 599820863),
}
SIZES = {599820856: 127357124, 599820861: 289942566, 599820862: 221432335, 599820863: 231295017}
RELEASE_ID = 399666322
REPOSITORY_ID = 1117899300
PROTECTED = {"freqinout.db": ("device_profiles", "js8_instances", "fast_light_configs"),
 "freqinout_nets.db": ("sop_profiles", "daily_schedule_tab", "net_schedule", "fldigi_checkins", "message_viewer_paths")}
SEED = '\nimport sqlite3\nfrom pathlib import Path\nfrom freqinout.version import __version__\nfrom freqinout.core.db_initializer import ensure_all_tables\nfrom freqinout.core.settings_manager import SettingsManager\nfrom freqinout.core.config_paths import get_config_dir\nfrom freqinout.core import multi_radio_store as m\nassert __version__ == "2.0.4"\nensure_all_tables()\ns = SettingsManager()\ns.set_many({\'callsign\': \'N0TEST\', \'grid\': \'DM79\', \'launch_control_enabled\': False, \'use_scheduler\': False, \'main_window_geometry\': \'synthetic-retained-geometry\'})\nassert m.get_multi_rig_migration_version(s._conn) == 3\nnow = "2026-10-12T00:00:00Z"\nfor index, name in enumerate(("FT-710", "FTDX-10")):\n    root = "C:/Radio Profiles/" + name\n    js8 = s._conn.execute("INSERT INTO js8_instances(system_key,name,port,profile_path,install_path,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?)",\n        ("test-js8-"+name, name+" JS8", 2442+index, root+"/JS8Call", "C:/Program Files/JS8Call/JS8Call.exe", now, now)).lastrowid\n    fast = s._conn.execute("INSERT INTO fast_light_configs(system_key,name,flrig_port,fldigi_port,flrig_path,fldigi_path,fldigi_log_path,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?,?)",\n        ("test-fast-"+name, name+" Fast Light", 12345+index, 7362+index, "C:/Radio Apps/flrig.exe", "C:/Radio Apps/fldigi.exe", root+"/fldigi/logs", now, now)).lastrowid\n    s._conn.execute("INSERT INTO device_profiles(system_key,name,radio_manufacturer,radio_model,js8_instance_id,fast_light_config_id,js8_port,flrig_port,fldigi_port,launch_enabled,scheduler_enabled,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",\n        ("test-radio-"+name, name, "Yaesu", name, js8, fast, 2442+index, 12345+index, 7362+index, 0, 0, now, now))\ns._conn.commit()\ns.close()\np = get_config_dir()\nwith sqlite3.connect(p / "config/freqinout_nets.db") as db:\n    db.execute("INSERT INTO sop_profiles(name,operating_group,frequency,sop_start_utc) VALUES(\'Retained SOP\',\'TEST\',\'7.110\',\'2026-10-12T01:00:00Z\')")\n    db.execute("INSERT INTO daily_schedule_tab(day_utc,band,mode,frequency,start_utc,end_utc,group_name) VALUES(\'Monday\',\'40m\',\'USB\',\'7.110\',\'01:00\',\'02:00\',\'TEST NET\')")\n    db.execute("INSERT INTO net_schedule(day_utc,band,mode,frequency,start_utc,end_utc,early_checkin,comment) VALUES(\'Tuesday\',\'80m\',\'USB\',\'3.580\',\'03:00\',\'04:00\',15,\'retained net\')")\n    db.executemany("INSERT INTO fldigi_checkins(callsign,last_seen_ts) VALUES(?,?)", [(f"N0T{n:03d}",1700000000+n) for n in range(100)])\n    db.execute("INSERT INTO message_viewer_paths(origin,path) VALUES(\'FLMSG\',\'C:/Radio Profiles/FT-710/NBEMS/messages\')")\n(p / "messages").mkdir()\n(p / "messages/retained-message.txt").write_text("Synthetic retained message\\n", encoding="utf-8")\nfor filename in ("freqinout.db", "freqinout_nets.db"):\n    with sqlite3.connect(p / "config" / filename) as db:\n        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")\n'


def checked(arguments, **kwargs):
    return subprocess.run([str(a) for a in arguments], check=True, timeout=300, **kwargs)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b""): value.update(chunk)
    return value.hexdigest()


def hashes(root):
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob("*")) if p.is_file()}


def snapshot(profile, columns=None):
    result = {}
    for filename, tables in PROTECTED.items():
        with closing(sqlite3.connect(profile/"config"/filename)) as db:
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("Station SQLite verification failed")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("Station relationship verification failed")
            for table in tables:
                key = filename+":"+table
                selected = columns[key][0] if columns else [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
                names = ",".join('"'+c+'"' for c in selected)
                rows = sorted(db.execute(f'SELECT {names} FROM "{table}"').fetchall(),key=repr)
                if not rows: raise RuntimeError("Synthetic station table is unexpectedly empty: "+table)
                result[key] = (selected, rows)
    return result


def seed_profile(work):
    source = work/"exact-2.0.4-source"
    source.mkdir()
    exists = subprocess.run(["git","cat-file","-e",HISTORICAL_COMMIT+"^{commit}"],cwd=ROOT,capture_output=True)
    if exists.returncode:
        checked(["git","fetch","--no-tags","--depth=1","origin",HISTORICAL_COMMIT],cwd=ROOT)
    archive = subprocess.check_output(["git","archive",HISTORICAL_COMMIT],cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise RuntimeError("Unsafe historical source archive member")
        tar.extractall(source)
    profile = work/"synthetic-station"
    env = dict(os.environ,FREQINOUT_CONFIG_DIR=str(profile),QT_QPA_PLATFORM="offscreen",PYTHONPATH=str(source),PYTHONDONTWRITEBYTECODE="1")
    fence = "import socket\ndef refuse(*a, **k): raise RuntimeError('Network forbidden during synthetic fixture creation')\nsocket.socket.connect=refuse\nsocket.socket.connect_ex=refuse\nsocket.socket.sendto=refuse\nsocket.create_connection=refuse\n"
    checked([sys.executable,"-c",fence+SEED],cwd=source,env=env)
    return profile


def launch(executable, profile, work, label, *, expected=0):
    marker = profile/"packaged-smoke-test.ok"
    marker.unlink(missing_ok=True)
    env = dict(os.environ,FREQINOUT_CONFIG_DIR=str(profile),FREQINOUT_ALLOW_EXTERNAL_RUNTIME_ENV="1",QT_QPA_PLATFORM="offscreen",FREQINOUT_HARD_EXIT="0")
    output = work/(label+".log")
    with output.open("wb") as handle:
        proc = subprocess.Popen([str(executable),"--smoke-test"],env=env,stdout=handle,stderr=subprocess.STDOUT)
        deadline = time.monotonic()+120
        try:
            while proc.poll() is None and not marker.exists() and time.monotonic()<deadline:
                time.sleep(.1)
            if expected:
                proc.wait(timeout=max(1,deadline-time.monotonic()))
                if proc.returncode != expected or marker.exists():
                    raise RuntimeError(f"{label}: expected refusal {expected}, got {proc.returncode}; see {output}")
            elif not marker.exists():
                raise RuntimeError(f"{label}: application not ready; see {output}")
            elif sys.platform != "win32":
                if proc.wait(timeout=30) != 0: raise RuntimeError(f"{label}: shutdown failed; see {output}")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=15)
                except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=15)
    # Windows smoke mode stays open until the supervising test stops it.
    lock = profile/"freqinout.lock"
    if lock.exists():
        try: pid=int(lock.read_text().splitlines()[0])
        except (ValueError,IndexError): raise RuntimeError("Malformed test lock")
        if pid != proc.pid: raise RuntimeError("Unexpected test lock owner")
        lock.unlink()


def verify_historical_asset(filename, expected, asset_id):
    def api(url):
        request = urllib.request.Request(url, headers={"Accept":"application/vnd.github+json", "User-Agent":"CommsPlan-native-qualification"})
        token = os.environ.get("GH_TOKEN")
        if token: request.add_header("Authorization", "Bearer "+token)
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    repo = api("https://api.github.com/repos/N1MAG/FreqInOut")
    if repo.get("id") != REPOSITORY_ID: raise RuntimeError("Historical repository identity changed")
    release = api(f"https://api.github.com/repos/N1MAG/FreqInOut/releases/{RELEASE_ID}")
    if release.get("id") != RELEASE_ID or release.get("tag_name") != "v2.0.4":
        raise RuntimeError("Historical release identity changed")
    asset = next((a for a in release.get("assets", []) if a.get("id") == asset_id), None)
    if not asset or asset.get("name") != filename or asset.get("size") != SIZES[asset_id] or asset.get("digest") != "sha256:"+expected:
        raise RuntimeError("Historical release asset identity/digest changed")
    return asset["browser_download_url"]


def download_historical(url, target, expected_size):
    with urllib.request.urlopen(url, timeout=60) as source, target.open("wb") as destination:
        remaining = expected_size
        while remaining:
            chunk = source.read(min(1024*1024, remaining))
            if not chunk: raise RuntimeError("Historical package download was truncated")
            destination.write(chunk)
            remaining -= len(chunk)
        if source.read(1): raise RuntimeError("Historical package exceeds pinned size")


def stop_test_process(proc, profile):
    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=15)
        except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=15)
    lock = profile/"freqinout.lock"
    if lock.exists() and int(lock.read_text().splitlines()[0]) == proc.pid:
        lock.unlink()


def active_application(executable, profile, work, label):
    station_log = profile/"freqinout.log"
    offset = station_log.stat().st_size if station_log.exists() else 0
    env = dict(os.environ,FREQINOUT_CONFIG_DIR=str(profile),FREQINOUT_ALLOW_EXTERNAL_RUNTIME_ENV="1",QT_QPA_PLATFORM="offscreen",FREQINOUT_HARD_EXIT="0",FREQINOUT_PERF_METRICS="1")
    output = (work/(label+".log")).open("wb")
    proc = subprocess.Popen([str(executable)],env=env,stdout=output,stderr=subprocess.STDOUT)
    output.close()
    deadline = time.monotonic()+120
    while proc.poll() is None and time.monotonic()<deadline:
        if station_log.exists() and b" started." in station_log.read_bytes()[offset:]:
            if int((profile/"freqinout.lock").read_text().splitlines()[0]) != proc.pid:
                stop_test_process(proc,profile)
                raise RuntimeError("Active test application did not own its profile lock")
            return proc, env
        time.sleep(.1)
    stop_test_process(proc,profile)
    raise RuntimeError("Active application readiness failed: "+label)


def check_running_refusal(executable, package, profile, work, baseline, *, uninstall=False):
    if sys.platform == "darwin": return  # Manual DMG procedure is separately governed.
    proc, _ = active_application(executable,profile,work,"active-"+("uninstall" if uninstall else executable.name))
    try:
        installed_root = executable.parent if sys.platform == "win32" else Path("/opt/freqinout/FreqInOut")
        installed = hashes(installed_root)
        if sys.platform == "win32":
            arguments = [executable.parent/"unins000.exe","/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART"] if uninstall else [package,"/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART",f"/DIR={executable.parent}",f"/LOG={work/'running-installer-refusal.log'}"]
        else:
            arguments = ["sudo","apt-get","remove","--yes","freqinout"] if uninstall else ["sudo","apt-get","install","--reinstall","--yes",package]
        result = subprocess.run([str(a) for a in arguments],timeout=180)
        if result.returncode == 0 or proc.poll() is not None:
            raise RuntimeError("Package operation did not refuse the running station")
        if hashes(installed_root) != installed or snapshot(profile,baseline) != baseline:
            raise RuntimeError("Refused package operation changed binaries or station rows")
    finally: stop_test_process(proc,profile)


def check_shared_lock(first, second, pristine, work, label):
    profile = work/label
    shutil.copytree(pristine,profile,ignore=shutil.ignore_patterns("backups","freqinout.lock","packaged-smoke-test.ok",".commsplan-packaged-startup.json"))
    before = snapshot(profile)
    owner, env = active_application(first,profile,work,label+"-owner")
    contender = None
    try:
        perf = profile/"perf_metrics.log"
        offset = perf.stat().st_size if perf.exists() else 0
        with (work/(label+"-contender.log")).open("wb") as handle:
            contender = subprocess.Popen([str(second),"--smoke-test"],env=env,stdout=handle,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if perf.exists() and b"startup.single_instance_lock" in perf.read_bytes()[offset:]: break
            if contender.poll() is not None: raise RuntimeError("Lock contender exited before lock-check evidence")
            time.sleep(.1)
        else: raise RuntimeError("Lock contender did not reach the lock check")
        if int((profile/"freqinout.lock").read_text().splitlines()[0]) != owner.pid or (profile/"packaged-smoke-test.ok").exists():
            raise RuntimeError("Concurrent old/new applications gained profile control")
        if snapshot(profile,before) != before: raise RuntimeError("Concurrent launch changed protected station rows")
    finally:
        if contender is not None: stop_test_process(contender,profile)
        stop_test_process(owner,profile)


def install_package(package, work, *, old=False):
    if sys.platform == "win32":
        appdir = work/"Installed"
        checked([package,"/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART",f"/DIR={appdir}"])
        return appdir/("FreqInOut.exe" if old else "CommsPlan.exe")
    if sys.platform.startswith("linux"):
        checked(["sudo","apt-get","install","--yes","--allow-downgrades",package])
        return Path("/usr/bin/freqinout" if old else "/usr/bin/commsplan")
    mount = work/("old-volume" if old else "new-volume")
    mount.mkdir(exist_ok=True)
    appname = "FreqInOut.app" if old else "CommsPlan.app"
    applications = work/"Applications"
    applications.mkdir(exist_ok=True)
    other = applications/("CommsPlan.app" if old else "FreqInOut.app")
    if other.exists():
        index = 1
        retained = work/("retained-"+other.name)
        while retained.exists():
            index += 1
            retained = work/(f"retained-{index}-"+other.name)
        other.rename(retained)
    app = applications/appname
    checked(["hdiutil","attach","-quiet","-nobrowse","-readonly","-mountpoint",mount,package])
    try:
        if app.exists(): shutil.rmtree(app)
        checked(["ditto",mount/appname,app])
    finally: checked(["hdiutil","detach","-quiet",mount])
    return app/"Contents/MacOS"/app.stem


def qualify(package, workspace, *, allow_local_macos=False):
    if os.environ.get("GITHUB_ACTIONS") != "true" and not (sys.platform == "darwin" and allow_local_macos):
        raise RuntimeError("Native package qualification is CI-only; it must never change an operator installation")
    package = Path(package).resolve(strict=True)
    work = Path(workspace).resolve()
    work.mkdir(parents=True,exist_ok=False)
    key = "windows" if sys.platform == "win32" else ("linux" if sys.platform.startswith("linux") else "macos-"+platform.machine().lower())
    filename,expected,asset_id = PINS[key]
    historical = work/filename
    url = verify_historical_asset(filename,expected,asset_id)
    download_historical(url,historical,SIZES[asset_id])
    if digest(historical) != expected: raise RuntimeError("Historical package SHA-256 mismatch")
    profile = seed_profile(work)
    before_rows = snapshot(profile)
    old = install_package(historical,work,old=True)
    launch(old,profile,work,"old-2.0.4-baseline")
    if snapshot(profile,before_rows) != before_rows: raise RuntimeError("Historical baseline changed synthetic station data")
    pristine = work/"pre-upgrade-profile"
    shutil.copytree(profile,pristine,ignore=shutil.ignore_patterns("backups","freqinout.lock","packaged-smoke-test.ok",".commsplan-packaged-startup.json"))
    if sys.platform != "darwin":
        old_root = old.parent if sys.platform == "win32" else Path("/opt/freqinout/FreqInOut")
        retained = work/"retained-old-runtime"
        shutil.copytree(old_root,retained,symlinks=True)
        old_copy = retained/("FreqInOut.exe" if sys.platform == "win32" else "FreqInOut")
    else:
        old_copy = work/"retained-FreqInOut.app/Contents/MacOS/FreqInOut"
    check_running_refusal(old,package,profile,work,before_rows)
    before = hashes(profile/"config")
    message = (profile/"messages/retained-message.txt").read_bytes()
    candidate = install_package(package,work)
    launch(candidate,profile,work,"candidate-upgrade")
    receipt = json.loads((profile/".commsplan-packaged-startup.json").read_text())
    backup = Path(receipt["backup"])
    if receipt["state"] != "verified-backup" or receipt["version"] != validated_version(ROOT) or hashes(backup/"config") != before:
        raise RuntimeError("Packaged backup did not capture exact pre-schema config bytes")
    if snapshot(profile,before_rows) != before_rows: raise RuntimeError("Upgrade changed protected station rows")
    check_shared_lock(old_copy,candidate,pristine,work,"lock-old-first")
    check_shared_lock(candidate,old_copy,pristine,work,"lock-new-first")
    check_running_refusal(candidate,package,profile,work,before_rows)
    launch(candidate,profile,work,"candidate-repeat")
    if json.loads((profile/".commsplan-packaged-startup.json").read_text()) != receipt:
        raise RuntimeError("Repeat startup did not reuse the verified backup")
    if (profile/"messages/retained-message.txt").read_bytes() != message: raise RuntimeError("Upgrade changed retained message")
    for scenario in ("corrupt", "partial"):
        damaged = work/scenario
        shutil.copytree(profile,damaged,ignore=shutil.ignore_patterns("backups","freqinout.lock","packaged-smoke-test.ok",".commsplan-packaged-startup.json"))
        if scenario == "corrupt": (damaged/"config/freqinout.db").write_bytes(b"invalid database")
        else: (damaged/"config/freqinout_nets.db").unlink()
        unchanged = hashes(damaged/"config")
        launch(candidate,damaged,work,scenario+"-refusal",expected=2)
        if hashes(damaged/"config") != unchanged: raise RuntimeError("Refused profile was changed")
    restored = work/"restored-station"
    shutil.copytree(profile,restored,ignore=shutil.ignore_patterns("config","backups","freqinout.lock","packaged-smoke-test.ok",".commsplan-packaged-startup.json"))
    shutil.copytree(backup/"config",restored/"config")
    if hashes(restored/"config") != before: raise RuntimeError("Cold restore did not retain backup bytes")
    rollback = install_package(historical,work,old=True)
    launch(rollback,restored,work,"old-2.0.4-rollback")
    if snapshot(restored,before_rows) != before_rows: raise RuntimeError("Old executable rollback changed station rows")
    # Reinstall the candidate to exercise a real package uninstall after rollback.
    candidate = install_package(package,work)
    launch(candidate,profile,work,"candidate-after-rollback")
    check_running_refusal(candidate,package,profile,work,before_rows,uninstall=True)
    if sys.platform == "win32":
        checked([candidate.parent/"unins000.exe","/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART"])
    elif sys.platform.startswith("linux"):
        checked(["sudo","apt-get","remove","--yes","freqinout"])
    else:
        shutil.rmtree(work/"Applications/CommsPlan.app")
    if (profile/"messages/retained-message.txt").read_bytes() != message: raise RuntimeError("Uninstall changed profile message")
    if snapshot(profile,before_rows) != before_rows: raise RuntimeError("Uninstall changed station databases")
    result = {"status":"passed","historical_commit":HISTORICAL_COMMIT,"historical_asset_id":asset_id,"historical_sha256":expected,"candidate_sha256":digest(package),"platform":key,"protected_rows":{k:len(v[1]) for k,v in before_rows.items()},"backup":str(backup),"scope":"disposable synthetic native install, populated upgrade/reopen, corrupt/partial refusal, backup restore/old executable rollback, uninstall preservation; no hardware or live credentials"}
    shutil.copy2(backup/"manifest.json",work/"backup-manifest.json")
    (work/"protected-snapshot.json").write_text(json.dumps(before_rows,indent=2)+"\n")
    (work/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package",type=Path,required=True)
    parser.add_argument("--workspace",type=Path,required=True)
    parser.add_argument("--allow-local-macos",action="store_true")
    args=parser.parse_args()
    qualify(args.package,args.workspace,allow_local_macos=args.allow_local_macos)

if __name__ == "__main__": main()

"""Offline server migration: PostgreSQL dump and cold Docker volume archives."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
HELPER = "alpine:3.20"
CACHES = {"embedding-model-cache", "ollama-model-cache"}
REQUIRED = {"postgres-data", "qdrant-data", "rustfs-data"}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return run(*args, stdout=subprocess.PIPE, text=True).stdout.strip()


def compose(files, *args, **kwargs):
    command = ["docker", "compose"]
    for file in files:
        command.extend(["-f", file])
    return run(*command, "--profile", "*", *args, **kwargs)


def config(files):
    result = compose(files, "config", "--format", "json", stdout=subprocess.PIPE, text=True)
    return json.loads(result.stdout)


def exists(volume):
    result = subprocess.run(["docker", "volume", "inspect", volume],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def prepare_helper():
    result = subprocess.run(["docker", "image", "inspect", HELPER],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        run("docker", "pull", HELPER)


def helper(volume, directory, command, *, readonly=True):
    mount = f"type=volume,src={volume},dst=/data,volume-nocopy"
    if readonly:
        mount += ",readonly"
    return run("docker", "run", "--rm", "--network", "none", "--mount", mount,
               "--mount", f"type=bind,src={directory},dst=/backup", HELPER,
               "sh", "-ec", command)


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def images(cfg):
    return {name: spec["image"] for name, spec in cfg["services"].items() if "image" in spec}


def check_config(cfg):
    if not REQUIRED <= cfg["volumes"].keys():
        raise ValueError("Compose must contain postgres-data, qdrant-data and rustfs-data")
    if any(spec.get("external") or spec.get("driver", "local") != "local"
           or spec.get("driver_opts") for spec in cfg["volumes"].values()):
        raise ValueError("Only local Docker volumes without driver options are supported")


def backup(files, cfg, directory):
    for key in REQUIRED:
        if not exists(cfg["volumes"][key]["name"]):
            raise ValueError(f"Source volume is missing: {key}")
    postgres = compose(files, "ps", "--status", "running", "-q", "postgres",
                       stdout=subprocess.PIPE, text=True).stdout.strip()
    if not postgres:
        raise ValueError("Start postgres before backup; no services were stopped")
    # Pull before downtime, and never reuse/overwrite an earlier backup directory.
    prepare_helper()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    if (ROOT / ".env").is_file():
        shutil.copyfile(ROOT / ".env", directory / ".env")
        (directory / ".env").chmod(0o600)
    print("Stopping services. They remain stopped after backup, including on failure.", flush=True)
    compose(files, "stop", "--timeout", "120",
            *(name for name in cfg["services"] if name != "postgres"))
    with (directory / "postgres.dump").open("wb") as stream:
        compose(files, "exec", "-T", "postgres", "sh", "-ec",
                'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc', stdout=stream)
    compose(files, "stop", "--timeout", "120", "postgres")
    archived = []
    for key, spec in cfg["volumes"].items():
        if key == "postgres-data" or key in CACHES or not exists(spec["name"]):
            continue
        print(f"Archiving {key}", flush=True)
        helper(spec["name"], directory, f"tar czpf /backup/{key}.tar.gz -C /data .")
        archived.append(key)
    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "architecture": output("docker", "info", "--format", "{{.Architecture}}"),
        "images": images(cfg),
        "volumes": archived,
        "files": {p.name: digest(p) for p in directory.iterdir() if p.is_file()},
    }
    # Written last: incomplete backups have no manifest and cannot be restored.
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Backup ready: {directory}\nServices remain stopped. To resume: just up-server")


def validate_backup(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        raise ValueError("Unsupported backup format")
    keys = manifest["volumes"]
    if len(set(keys)) != len(keys) or any(
        not isinstance(key, str) or not key.replace("-", "").isalnum() for key in keys
    ) or "postgres-data" in keys:
        raise ValueError("Invalid volume list")
    required_files = {"postgres.dump", *(f"{key}.tar.gz" for key in keys)}
    if not required_files <= manifest["files"].keys():
        raise ValueError("Backup manifest is missing required files")
    if not {"qdrant-data", "rustfs-data"} <= set(keys):
        raise ValueError("Backup is missing Qdrant or RustFS")
    for name, expected in manifest["files"].items():
        if Path(name).name != name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError("Invalid backup filename")
        if digest(directory / name) != expected:
            raise ValueError(f"Checksum mismatch: {name}")
    return manifest


def restore(files, cfg, directory):
    manifest = validate_backup(directory)
    if manifest["architecture"] != output("docker", "info", "--format", "{{.Architecture}}"):
        raise ValueError("Cold volume restore requires the same server architecture")
    if images(cfg) != manifest["images"]:
        raise ValueError("Use the same project revision and service image versions as the source")
    keys = ["postgres-data", *manifest["volumes"]]
    if not set(keys) <= cfg["volumes"].keys():
        raise ValueError("Target Compose configuration is missing backup volumes")
    if output("docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={cfg['name']}"):
        raise ValueError("Target project must be stopped before restoring")
    prepare_helper()
    # Check EVERY target before writing anything. Never erase existing data.
    for key in keys:
        volume = cfg["volumes"][key]["name"]
        if not exists(volume):
            continue
        if output("docker", "ps", "-q", "--filter", f"volume={volume}"):
            raise ValueError(f"Volume is in use: {volume}")
        helper(volume, directory,
               'test -z "$(find /data -mindepth 1 -maxdepth 1 -print -quit)" || '
               '{ echo "Target volume is not empty" >&2; exit 1; }')
    for key in keys:
        volume = cfg["volumes"][key]["name"]
        if not exists(volume):
            run("docker", "volume", "create", "--label", f"com.docker.compose.project={cfg['name']}",
                "--label", f"com.docker.compose.volume={key}", volume)
    for key in manifest["volumes"]:
        helper(cfg["volumes"][key]["name"], directory,
               f"tar xzpf /backup/{key}.tar.gz -C /data", readonly=False)
    # Start only a fresh PostgreSQL, without migrations or application writers.
    try:
        compose(files, "up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", "postgres")
        with (directory / "postgres.dump").open("rb") as stream:
            compose(files, "exec", "-T", "postgres", "sh", "-ec",
                    'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                    '--no-owner --no-acl --single-transaction --exit-on-error', stdin=stream)
    finally:
        compose(files, "stop", "--timeout", "120", "postgres")
    print("Restore complete. Start the project: just up-server")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("directory", nargs="?", default="")
    parser.add_argument("-f", "--file", action="append", dest="files",
                        help="Compose file; repeat for overrides (default: base + server)")
    args = parser.parse_args()
    os.chdir(ROOT)
    files = args.files or ["compose.yaml", "compose.server.yaml"]
    if args.action == "restore" and not args.directory:
        parser.error("restore requires a backup directory")
    directory = Path(args.directory or datetime.now().strftime("backups/%Y%m%d-%H%M%S")).resolve()
    cfg = config(files)
    check_config(cfg)
    if args.action == "backup":
        backup(files, cfg, directory)
    else:
        restore(files, cfg, directory)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Migration failed: {error}\nServices are not automatically restarted.", file=sys.stderr)
        sys.exit(1)

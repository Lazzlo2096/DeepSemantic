"""Isolated Docker round-trip test; never uses the application's volumes.

Run: python scripts/test_migrate.py
"""

import json
from pathlib import Path
import tempfile
import uuid

import migrate


def main():
    suffix = uuid.uuid4().hex[:10]
    projects = [f"migration-test-{suffix}-source", f"migration-test-{suffix}-target"]
    configs = []
    with tempfile.TemporaryDirectory(prefix="deepsemantic-migration-") as temporary:
        directory = Path(temporary)
        try:
            for project in projects:
                path = directory / f"{project}.json"
                path.write_text(json.dumps({
                    "name": project,
                    "services": {"postgres": {
                        "image": "postgres:17-alpine",
                        "environment": {"POSTGRES_USER": "migration", "POSTGRES_PASSWORD": "test",
                                        "POSTGRES_DB": "migration"},
                        "volumes": ["postgres-data:/var/lib/postgresql/data"],
                        "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U migration"],
                                        "interval": "1s", "timeout": "3s", "retries": 60},
                    }, "storage": {
                        "image": migrate.HELPER,
                        "command": ["sleep", "infinity"],
                        "volumes": ["qdrant-data:/qdrant", "rustfs-data:/rustfs"],
                    }},
                    "volumes": {key: {} for key in migrate.REQUIRED},
                }), encoding="utf-8")
                files = [str(path)]
                cfg = migrate.config(files)
                configs.append((files, cfg))
            files, cfg = configs[0]
            migrate.compose(files, "up", "-d", "--wait", "postgres")
            migrate.compose(files, "exec", "-T", "postgres", "psql", "-U", "migration", "-d", "migration",
                            "-v", "ON_ERROR_STOP=1", "-c",
                            "CREATE TABLE probe (id serial PRIMARY KEY, value text); "
                            "INSERT INTO probe(value) VALUES ('migration works');")
            for key in ("qdrant-data", "rustfs-data"):
                volume = cfg["volumes"][key]["name"]
                migrate.run("docker", "volume", "create", volume)
                migrate.helper(volume, directory,
                               "printf 'volume contents' > /data/probe; chmod 640 /data/probe; "
                               "chown 1001:1002 /data/probe", readonly=False)
            backup = directory / "backup"
            migrate.backup(files, cfg, backup)
            manifest = migrate.validate_backup(backup)
            assert set(manifest["volumes"]) == {"qdrant-data", "rustfs-data"}
            archive = backup / "qdrant-data.tar.gz"
            contents = archive.read_bytes()
            archive.write_bytes(contents + b"broken")
            try:
                migrate.validate_backup(backup)
            except ValueError as error:
                assert "Checksum mismatch" in str(error)
            else:
                raise AssertionError("Corrupt backup was accepted")
            archive.write_bytes(contents)
            target_files, target_cfg = configs[1]
            migrate.restore(target_files, target_cfg, backup)
            for key in ("qdrant-data", "rustfs-data"):
                migrate.helper(target_cfg["volumes"][key]["name"], directory,
                               'test "$(cat /data/probe)" = "volume contents"; '
                               'test "$(stat -c %a:%u:%g /data/probe)" = "640:1001:1002"')
            # A second restore must refuse existing data before changing it.
            try:
                migrate.restore(target_files, target_cfg, backup)
            except Exception as error:
                assert isinstance(error, migrate.subprocess.CalledProcessError)
            else:
                raise AssertionError("Nonempty target was accepted")
            migrate.compose(target_files, "up", "-d", "--wait", "postgres")
            result = migrate.compose(target_files, "exec", "-T", "postgres", "psql", "-U", "migration",
                                     "-d", "migration", "-Atc", "SELECT value FROM probe",
                                     stdout=migrate.subprocess.PIPE, text=True)
            assert result.stdout.strip() == "migration works"
            migrate.compose(target_files, "exec", "-T", "postgres", "psql", "-U", "migration",
                            "-d", "migration", "-v", "ON_ERROR_STOP=1", "-c",
                            "INSERT INTO probe(value) VALUES ('sequence works'); "
                            "DO $$ BEGIN IF (SELECT max(id) FROM probe) != 2 THEN "
                            "RAISE EXCEPTION 'Sequence lost'; END IF; END $$;")
            print("PASS: PostgreSQL rows/sequences, volume contents/UID/mode, corruption and overwrite guards")
        finally:
            for files, cfg in configs:
                migrate.compose(files, "down", "--volumes")
                for spec in cfg["volumes"].values():
                    if migrate.exists(spec["name"]):
                        migrate.run("docker", "volume", "rm", spec["name"])


if __name__ == "__main__":
    main()

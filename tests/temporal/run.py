"""Reproducible integration suite, isolated from the application's project/data."""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ['docker', 'compose', '-f', str(ROOT / 'tests/temporal/compose.yaml')]


def run(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def main():
    try:
        run('docker', 'build', '-f', 'temporal/Dockerfile', '-t', 'deepsemantic-processing:local', '.')
        run(*COMPOSE, 'up', '-d', '--wait', 'postgres', 'temporal-postgres', 'temporal', 'qdrant', 'rustfs')
        run(*COMPOSE, 'run', '--rm', '--build', 'migrate', 'upgrade', '0037_rag_run_metadata')
        with (ROOT / 'tests/temporal/seed_legacy.sql').open('rb') as seed:
            run(*COMPOSE, 'exec', '-T', 'postgres', 'psql', '-U', 'test', '-d', 'deepsemantic_test', '-v', 'ON_ERROR_STOP=1', stdin=seed)
        run(*COMPOSE, 'run', '--rm', 'migrate', 'upgrade', 'head')
        run(*COMPOSE, 'run', '--rm', '--no-deps', 'tests', 'python', '-m', 'unittest', 'discover', '-s', 'tests', '-v')
        run(*COMPOSE, 'run', '--rm', '--no-deps', 'tests', 'python', 'tests/temporal/integration.py')
    finally:
        # Only this explicitly named, disposable test project is removed.
        run(*COMPOSE, 'down', '--volumes', '--remove-orphans')


if __name__ == '__main__':
    main()

"""Validate provider profiles without starting containers or requiring a daemon."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("docker"), "Docker CLI is required for Compose validation")
class ComposeProfileTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("just"), "just is required to validate recipes")
    def test_just_start_recipes_select_provider_profile(self):
        for provider in ("deepkaravan", "openrouter"):
            for recipe in ("up", "up-server", "restart", "build", "langsmith-studio-up"):
                with self.subTest(provider=provider, recipe=recipe):
                    result = subprocess.run(
                        ["just", "--dry-run", recipe], cwd=ROOT,
                        env=dict(os.environ, EMBEDDING_PROVIDER=provider),
                        capture_output=True, text=True, check=True,
                    )
                    self.assertIn(f"--profile {provider}", result.stdout + result.stderr)

    def config(self, provider, *, server=False, extras=False):
        env = dict(os.environ, EMBEDDING_PROVIDER=provider)
        env.pop("COMPOSE_PROFILES", None)
        command = [
            "docker", "compose", "--project-name", "deepsemantic-profile-test",
            "--env-file", str(ROOT / ".env.example"), "-f", str(ROOT / "compose.yaml"),
        ]
        if server:
            command += ["-f", str(ROOT / "compose.server.yaml")]
        if extras:
            # Explicit --profile replaces COMPOSE_PROFILES, as in the just recipes.
            command += ["--profile", provider, "--profile", "langsmith-studio"]
        command += ["config", "--format", "json"]
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_local_provider_enables_deepkaravan(self):
        for server in (False, True):
            for extras in (False, True):
                with self.subTest(server=server, extras=extras):
                    config = self.config("deepkaravan", server=server, extras=extras)
                    self.assertIn("deepkaravan", list(config["services"]))
                    self.assertIn("deepkaravan", list(config["services"]["api"]["depends_on"]))

    def test_cloud_provider_does_not_enable_deepkaravan(self):
        for server in (False, True):
            for extras in (False, True):
                with self.subTest(server=server, extras=extras):
                    config = self.config("openrouter", server=server, extras=extras)
                    self.assertNotIn("deepkaravan", list(config["services"]))
                    names = ("api", "temporal-orchestrator", "langsmith-studio") if extras else ("api", "temporal-orchestrator")
                    for name in names:
                        self.assertEqual(config["services"][name]["environment"]["EMBEDDING_PROVIDER"], "openrouter")

    def test_orchestrator_runs_all_queues_without_separate_worker_services(self):
        for server in (False, True):
            config = self.config("deepkaravan", server=server, extras=True)
            self.assertEqual(config["services"]["temporal-orchestrator"]["command"],
                             ["python", "-m", "processing.worker", "all"])
            self.assertFalse({"arxiv-grabber", "arxiv-text-worker", "embeding-worker", "summary-worker"}
                             & config["services"].keys())

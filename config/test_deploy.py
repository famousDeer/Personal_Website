"""Aktualizacja bez prawdziwego Git, Dockera i wdrażania."""
import os
import subprocess
import tempfile
from pathlib import Path
from django.test import SimpleTestCase


class DeployTests(SimpleTestCase):
    def test_current_build_does_not_fetch_or_merge(self):
        script = Path(__file__).resolve().parents[1] / 'deploy/update.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Izolujemy również plik blokady skryptu.
            copy = root / 'deploy/update.sh'
            copy.parent.mkdir()
            copy.write_text(script.read_text().replace('/tmp/personal_website-update.lock', str(root / 'lock')))
            for name, body in {
                'flock': 'exit 0',
                'git': 'echo "git $*" >> "$AUDIT_CALLS"; [ "$1" = rev-parse ] && echo abc123; exit 0',
                'docker': 'echo "docker $*" >> "$AUDIT_CALLS"; case "$*" in *"ps -q"*) echo container;; inspect*) echo healthy;; esac; exit 0',
            }.items():
                command = root / name
                command.write_text('#!/bin/sh\n' + body + '\n')
                command.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], AUDIT_CALLS=str(root / 'calls'))
            result = subprocess.run(['bash', str(copy), '--current'], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / 'calls').read_text()
            self.assertNotIn('git fetch', calls)
            self.assertNotIn('git merge', calls)
            self.assertIn('docker compose build', calls)
            self.assertIn('docker compose up', calls)

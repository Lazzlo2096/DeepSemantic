"""Contracts that can be checked without a Temporal Service or database."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'api'))

from processing.config import fingerprint


class ProcessingTests(unittest.TestCase):
    def test_work_identity_ignores_dict_order_and_changes_with_input_or_configuration(self):
        source = {'source_version': 'text-v1', 'model': 'model-a', 'max_tokens': 512}
        self.assertEqual(fingerprint(source), fingerprint(dict(reversed(list(source.items())))))
        for key, value in [('source_version', 'text-v2'), ('model', 'model-b'), ('max_tokens', 256)]:
            self.assertNotEqual(fingerprint(source), fingerprint({**source, key: value}))

    def test_backup_refuses_missing_temporal_history_before_stopping_services(self):
        spec = importlib.util.spec_from_file_location('migration_backup', ROOT / 'scripts/migrate.py')
        migrate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migrate)
        cfg = {'services': {'temporal': {}}, 'volumes': {
            key: {'name': key} for key in [*migrate.REQUIRED, 'temporal-postgres-data']
        }}
        with patch.object(migrate, 'exists', side_effect=lambda key: key != 'temporal-postgres-data'), \
                patch.object(migrate, 'compose') as compose:
            with self.assertRaisesRegex(ValueError, 'temporal-postgres-data'):
                migrate.backup([], cfg, Path('unused'))
            compose.assert_not_called()


if __name__ == '__main__':
    unittest.main()

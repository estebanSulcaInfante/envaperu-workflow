import json
import os
from pathlib import Path
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github' / 'workflows' / 'engineering-api-image.yml'
RECIPE = Path(__file__).resolve().parent


class EngineeringApiPreparationTests(unittest.TestCase):
    def test_exact_ci_gate_has_three_successful_jobs(self):
        gate = runpy.run_path(str(RECIPE / 'check_ci.py'), run_name='engineering_checks')
        self.assertEqual(gate['SOURCE_SHA'], '564be49ec362b574c873a30b33c4c5265dedb426')
        self.assertEqual(gate['RUN_ID'], 41476580052)
        self.assertEqual(gate['BRANCH'], 'codex/ingenieria-source-20261102')
        jobs = [
            {'name': name, 'status': 'completed', 'conclusion': 'success'}
            for name in gate['REQUIRED_JOBS']
        ]
        self.assertTrue(gate['validate_jobs']({'jobs': jobs}))
        with self.assertRaises(ValueError):
            gate['validate_jobs']({'jobs': jobs[:-1]})
        with self.assertRaises(ValueError):
            gate['validate_inputs'](
                gate['SOURCE_SHA'],
                'python:3.12-slim@sha256:' + 'a' * 64,
            )

    def test_allowlist_is_engineering_source_and_has_four_overlays(self):
        manifest = json.loads((RECIPE / 'runtime-allowlist.json').read_text())
        self.assertEqual(manifest['source_sha'], '564be49ec362b574c873a30b33c4c5265dedb426')
        overlays = {
            'app/api/rutas_scm.py',
            'app/services/scm_article_service.py',
            'app/services/scm_packaging_service.py',
            'app/services/scm_engineering_reads.py',
        }
        self.assertTrue(overlays.issubset(manifest['files']))
        self.assertNotIn('services/openclaw_access_bridge/src/server.js', manifest['files'])

    def test_workflow_is_api_only_and_keeps_protected_gate(self):
        workflow = WORKFLOW.read_text()
        self.assertIn('permissions: {}', workflow)
        self.assertIn('codex/ingenieria-api-recipe-20261102', workflow)
        self.assertIn('python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f', workflow)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.publish", workflow)
        self.assertIn("github.ref == 'refs/heads/codex/scm-contabo-image'", workflow)
        self.assertIn('environment: scm-pilot-image-public', workflow)
        self.assertIn('packages: write', workflow)
        self.assertEqual(workflow.count('environment: scm-pilot-image-public'), 1)
        self.assertIn('deploy/engineering-api/check_ci.py', workflow)
        self.assertIn('deploy/engineering-api/verify_image_layers.py', workflow)
        self.assertNotRegex(workflow, r'BRIDGE|NATIVE|openclaw')
        self.assertNotIn('docker push "$BRIDGE_IMAGE', workflow)


if __name__ == '__main__':
    unittest.main()

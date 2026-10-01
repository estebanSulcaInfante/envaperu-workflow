"""Offline tests: release gates and digest pinning, no credentials or network."""
import datetime
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreparationTests(unittest.TestCase):
    def test_exact_successful_main_ci(self):
        gate = load('check_ci')
        row = dict(head_sha='a'*40, run_number=2, run_attempt=1, status='completed',
                   conclusion='success', head_branch='main', event='push', html_url='https://example.invalid/run')
        self.assertEqual(gate.select_success({'workflow_runs':[row]}, 'a'*40), row['html_url'])
        for changed in [dict(row, head_sha='b'*40), dict(row, conclusion='failure'),
                        dict(row, event='pull_request'), dict(row, status='in_progress')]:
            with self.assertRaises(ValueError): gate.select_success({'workflow_runs':[changed]}, 'a'*40)
        with self.assertRaises(ValueError):
            gate.select_success({'workflow_runs':[row,dict(row,run_number=3,conclusion='failure')]}, 'a'*40)

    def test_inputs_are_full_sha_and_pinned_python(self):
        gate = load('check_ci')
        gate.validate_inputs('a'*40, 'python:3.12-slim@sha256:'+'b'*64)
        for sha, base in [('main', 'python:3.12-slim'), ('a'*40,'python:3.12-slim'),
                          ('a'*40, 'evil:3.12@sha256:'+'b'*64)]:
            with self.assertRaises(ValueError): gate.validate_inputs(sha,base)

    def test_release_preserves_secret_placeholders_and_rejects_tags(self):
        render = load('render_release').render
        evidence = dict(source_sha='a'*40, api_image='ghcr.io/estebansulcainfante/envaperu-scm-api@sha256:'+'b'*64)
        template = (ROOT/'compose.yaml').read_text()
        out = render(template,evidence,'postgres:16-alpine@sha256:'+'c'*64,'a'*40)
        self.assertNotIn('${SCM_API_IMAGE',out)
        self.assertIn('${SCM_API_ENV_FILE',out)
        self.assertIn('${SCM_PG_PASSWORD_FILE',out)
        for changed, pg, sha in [(dict(evidence,api_image='image:latest'),'postgres:16@sha256:'+'c'*64,'a'*40),
                                 (evidence,'postgres:18@sha256:'+'c'*64,'a'*40),
                                 (evidence,'postgres:16@sha256:'+'c'*64,'d'*40)]:
            with self.assertRaises(ValueError): render(template,changed,pg,sha)


if __name__ == '__main__':
    unittest.main()

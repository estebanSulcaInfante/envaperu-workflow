"""Offline regression tests for reviewed source, context, layers and publication gates."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).parent

def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

class PreparationTests(unittest.TestCase):
    def test_exact_reviewed_ci(self):
        gate=load('check_ci');sha=gate.SOURCE_SHA
        row=dict(id=gate.RUN_ID,head_sha=sha,head_branch=gate.BRANCH,event='push',path='.github/workflows/tests.yml',repository={'full_name':gate.REPOSITORY},status='completed',conclusion='success',html_url='https://example.invalid/run')
        self.assertEqual(gate.select_success(row,sha),row['html_url'])
        for key,value in [('id',1),('head_branch','main'),('head_sha','b'*40),('event','pull_request'),('conclusion','failure'),('path','other.yml')]:
            with self.assertRaises(ValueError):gate.select_success(dict(row,**{key:value}),sha)
        gate.validate_inputs(sha,'python:3.12-slim@sha256:'+'b'*64)
        with self.assertRaises(ValueError):gate.validate_inputs('a'*40,'python:3.12-slim@sha256:'+'b'*64)

    def test_context_ignores_unapproved_files_and_checks_bytes(self):
        builder=load('build_context')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';recipe=root/'recipe';source.mkdir();recipe.mkdir()
            (source/'run.py').write_bytes(b'print(1)\n');(source/'private.csv').write_bytes(b'not allowed')
            manifest={'source_sha':'a'*40,'files':{'run.py':hashlib.sha256(b'print(1)\n').hexdigest()}}
            (recipe/'runtime-allowlist.json').write_text(json.dumps(manifest))
            for f in ['Dockerfile','Dockerfile.dockerignore']:(recipe/f).write_text('test')
            with patch.object(builder.subprocess,'check_output',return_value='a'*40):
                builder.create_context(source,root/'context',recipe)
                self.assertFalse((root/'context/private.csv').exists())
                (source/'run.py').write_bytes(b'changed')
                with self.assertRaises(ValueError):builder.create_context(source,root/'bad',recipe)

    def test_all_layers_reject_unapproved_or_previously_deleted_file(self):
        verifier=load('verify_image_layers');body=b'print(1)\n'
        manifest={'files':{'run.py':hashlib.sha256(body).hexdigest()},'excluded_runtime_resources':['app/templates/example.csv']}
        def archive(path,entries):
            layer_bytes=io.BytesIO()
            with tarfile.open(fileobj=layer_bytes,mode='w') as layer:
                for name,data in entries:
                    info=tarfile.TarInfo(name);info.size=len(data);layer.addfile(info,io.BytesIO(data))
            with tarfile.open(path,'w') as image:
                for name,data in [('manifest.json',json.dumps([{'Layers':['layer.tar']}]).encode()),('layer.tar',layer_bytes.getvalue())]:
                    info=tarfile.TarInfo(name);info.size=len(data);image.addfile(info,io.BytesIO(data))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'image.tar';archive(p,[('app/run.py',body)]);verifier.verify(p,manifest)
            for extra in [[('app/.env',b'x')],[('app/example.csv',b'x')],[('app/.env',b'x'),('app/.wh..env',b'')],[('app/run.py',b'changed')],[('/app/.env',b'x')],[('app/../tmp/.env',b'x')]]:
                archive(p,[('app/run.py',body)]+extra)
                with self.assertRaises(ValueError):verifier.verify(p,manifest)

    def test_environment_gate(self):
        gate=load('check_publish_gate')
        env={'protection_rules':[{'type':'required_reviewers','reviewers':[{'id':1}]}],'deployment_branch_policy':{'custom_branch_policies':True}}
        policy={'branch_policies':[{'name':gate.BRANCH,'type':'branch'}]}
        gate.validate(env,policy)
        with self.assertRaises(ValueError):gate.validate({},policy)
        with self.assertRaises(ValueError):gate.validate(env,{'branch_policies':[{'name':'*'}]})

    def test_runtime_template_is_explicit_and_other_resources_stay_out(self):
        manifest=json.loads((ROOT/'runtime-allowlist.json').read_text())
        self.assertIn('app/templates/excel/OrdenProduccion/Book2.xlsx',manifest['files'])
        self.assertEqual(len(manifest['approved_templates']),1)
        self.assertEqual(len(manifest['excluded_runtime_resources']),8)

    def test_release_pins_images_and_preserves_secret_placeholders(self):
        renderer=load('render_release');sha='a'*40
        evidence={'source_sha':sha,'api_image':'ghcr.io/estebansulcainfante/envaperu-scm-api@sha256:'+'b'*64}
        template=(ROOT/'compose.yaml').read_text()
        out=renderer.render(template,evidence,'postgres:16@sha256:'+'c'*64,sha)
        self.assertIn('${SCM_API_ENV_FILE',out);self.assertNotIn('${SCM_API_IMAGE',out)
        with self.assertRaises(ValueError):renderer.render(template,evidence,'postgres:18@sha256:'+'c'*64,sha)

if __name__=='__main__':unittest.main()

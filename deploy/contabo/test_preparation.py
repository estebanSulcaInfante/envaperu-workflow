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

def _reject_duplicate_keys(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key]=value
    return result

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
        jobs=[{'name':name,'status':'completed','conclusion':'success'} for name in gate.REQUIRED_JOBS]
        self.assertTrue(gate.validate_jobs({'jobs':jobs}))
        with self.assertRaises(ValueError):gate.validate_jobs({'jobs':jobs[:-1]})

    def test_context_ignores_unapproved_files_and_checks_bytes(self):
        builder=load('build_context')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';recipe=root/'recipe';source.mkdir();recipe.mkdir()
            (source/'run.py').write_bytes(b'print(1)\n');(source/'private.csv').write_bytes(b'not allowed')
            manifest={'source_sha':'a'*40,'files':{'run.py':hashlib.sha256(b'print(1)\n').hexdigest()}}
            (recipe/'runtime-allowlist.json').write_text(json.dumps(manifest))
            for f in ['Dockerfile','Dockerfile.dockerignore']:(recipe/f).write_text('test')
            def git_output(command, **_):
                return 'a'*40 if 'rev-parse' in command else (source/'run.py').read_bytes()
            with patch.object(builder.subprocess,'check_output',side_effect=git_output):
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
        manifest=json.loads(
            (ROOT/'runtime-allowlist.json').read_text(),
            object_pairs_hook=_reject_duplicate_keys,
        )
        self.assertIn('app/templates/excel/OrdenProduccion/Book2.xlsx',manifest['files'])
        self.assertEqual(len(manifest['approved_templates']),1)
        self.assertEqual(len(manifest['excluded_runtime_resources']),8)

    def test_bridge_allowlist_is_separate_and_dependency_pinned(self):
        manifest=json.loads(
            (ROOT/'bridge-runtime-allowlist.json').read_text(),
            object_pairs_hook=_reject_duplicate_keys,
        )
        self.assertEqual(manifest['source_sha'],load('check_ci').SOURCE_SHA)
        self.assertEqual(manifest['source_prefix'],'services/openclaw_access_bridge')
        self.assertEqual(len(manifest['files']),10)
        self.assertEqual(len(manifest['context_files']),2)
        self.assertEqual(manifest['dependency_packages'],{'ws':'8.22.0'})
        self.assertNotIn('node_modules',manifest['files'])
        self.assertTrue(all(not path.startswith('test/') for path in manifest['files']))

    def test_workflow_builds_and_publishes_three_images_under_one_gate(self):
        workflow=(ROOT.parent.parent/'.github'/'workflows'/'contabo-image.yml').read_text()
        self.assertIn('scm-pilot-image-public',workflow)
        self.assertEqual(workflow.count('environment: scm-pilot-image-public'),1)
        self.assertIn('verify_image_layers.py api.tar',workflow)
        self.assertIn('verify_bridge_layers.py bridge.tar',workflow)
        self.assertIn('verify_native_layers.py native.tar',workflow)
        self.assertIn('--base-archive openclaw-base.tar',workflow)
        self.assertIn("node-version: '24'",workflow)
        self.assertIn('OPENCLAW_SOURCE_ROOT: ${{ github.workspace }}/openclaw-source',workflow)
        self.assertIn('src/components/login-gate.test.ts',workflow)
        self.assertIn('src/components/app-sidebar-session-catalog-live.test.ts',workflow)
        self.assertIn('src/pages/chat/chat-pane-pull-requests.test.ts',workflow)
        self.assertIn('src/app/browser-auth-recovery.test.ts',workflow)
        self.assertIn('for scm_flag in false true; do',workflow)
        self.assertIn('VITE_SCM_PERSONAL_ACCESS="$scm_flag"',workflow)
        self.assertIn('OPENCLAW_VITEST_FS_MODULE_CACHE_PATH="$RUNNER_TEMP/openclaw-vitest-fs-cache-$scm_flag"',workflow)
        self.assertIn('actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38',workflow)
        self.assertNotIn('uses: actions/setup-node@v6',workflow)
        self.assertIn('docker push "$api_tag"',workflow)
        self.assertIn('docker push "$bridge_tag"',workflow)
        self.assertIn('docker push "$native_tag"',workflow)
        self.assertIn('BRIDGE_IMAGE: ghcr.io/estebansulcainfante/envaperu-scm-api',workflow)
        self.assertIn('bridge-sha-$SOURCE_SHA-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT',workflow)
        self.assertIn('docker --config "$anon_config" pull "$API_DIGEST"',workflow)
        self.assertIn('docker --config "$anon_config" pull "$BRIDGE_DIGEST"',workflow)
        self.assertIn('docker --config "$anon_config" pull "$NATIVE_DIGEST"',workflow)

    def test_bridge_config_verifier_rejects_root_and_secret_env(self):
        verifier=load('verify_bridge_layers')
        def config(user='node',env=None):
            return {'rootfs':{'type':'layers'},'config':{
                'User':user,'WorkingDir':'/app','Cmd':['node','src/server.js'],
                'Env':env or ['PATH=/usr/bin','NODE_ENV=production','SCM_BRIDGE_HOST=0.0.0.0','SCM_BRIDGE_PORT=5182'],
            }}
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'image.tar'
            with tarfile.open(p,'w') as archive:
                for name,body in [('config.json',json.dumps(config()).encode())]:
                    info=tarfile.TarInfo(name); info.size=len(body); archive.addfile(info,io.BytesIO(body))
            with tarfile.open(p,'r') as archive:
                verifier._load_lockfile(archive,'config.json')
            for bad in [config(user='0'),config(env=['BRIDGE_SECRET=leak'])]:
                with tarfile.open(p,'w') as archive:
                    body=json.dumps(bad).encode(); info=tarfile.TarInfo('config.json'); info.size=len(body); archive.addfile(info,io.BytesIO(body))
                with tarfile.open(p,'r') as archive:
                    with self.assertRaises(ValueError): verifier._load_lockfile(archive,'config.json')

    def test_native_overlay_verifier_seals_only_control_ui_layer(self):
        verifier=load('verify_native_layers')
        metadata={'baseImage':'ghcr.io/openclaw/openclaw@sha256:'+'a'*64,
                  'upstream':{'version':'2026.9.8','revision':'b'*40,'patchSha256':'c'*64},
                  'overlay':{'buildId':'overlay-build','builtAt':'2026-10-30T19:57:28.135Z'}}
        labels={'org.opencontainers.image.version':'2026.9.8',
                'org.opencontainers.image.revision':'b'*40,
                'io.envaperu.openclaw.base-image':metadata['baseImage'],
                'io.envaperu.openclaw.patch.sha256':'c'*64,
                'io.envaperu.openclaw.control-ui.build-id':'overlay-build',
                'io.envaperu.openclaw.control-ui.built-at':'2026-10-30T19:57:28.135Z'}
        def layer(entries):
            out=io.BytesIO()
            with tarfile.open(fileobj=out,mode='w') as archive:
                for name,data in entries:
                    info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
            return out.getvalue()
        def write_archive(path, overlay_entries, base=False):
            config_name='config.json';base_name='base.tar';overlay_names=['overlay-0.tar','overlay-1.tar']
            config={'config':{'Labels':labels,'Env':['PATH=/usr/bin']},'rootfs':{'type':'layers','diff_ids':['sha256:base'] if base else ['sha256:base','sha256:overlay-0','sha256:overlay-1']}}
            layers=[base_name] if base else [base_name,*overlay_names]
            with tarfile.open(path,'w') as image:
                entries=[(config_name,json.dumps(config).encode()),(base_name,layer([('usr/bin/node',b'node')]))]
                entries.extend((name,layer([entry])) for name,entry in zip(overlay_names,overlay_entries))
                for name,data in entries:
                    if base and name != config_name and name != base_name: continue
                    info=tarfile.TarInfo(name);info.size=len(data);image.addfile(info,io.BytesIO(data))
                data=json.dumps([{'Config':config_name,'Layers':layers}]).encode();info=tarfile.TarInfo('manifest.json');info.size=len(data);image.addfile(info,io.BytesIO(data))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'native.tar'
            base=Path(d)/'base.tar'
            write_archive(base,[('usr/bin/node',b'node')],base=True)
            write_archive(p,[('app/dist/control-ui/index.html',b'ui'),('app/dist/build-info.json',b'{}')])
            verifier.verify(p,metadata,base)
            with self.assertRaises(ValueError):
                verifier.verify(p, {**metadata, 'overlay': {'buildId': '', 'builtAt': metadata['overlay']['builtAt']}}, base)
            write_archive(p,[('app/etc/config.json',b'bad'),('app/dist/build-info.json',b'{}')])
            with self.assertRaises(ValueError):verifier.verify(p,metadata,base)

    def test_release_pins_images_and_preserves_secret_placeholders(self):
        renderer=load('render_release');sha='a'*40
        evidence={'source_sha':sha,'api_image':'ghcr.io/estebansulcainfante/envaperu-scm-api@sha256:'+'b'*64}
        template=(ROOT/'compose.yaml').read_text()
        out=renderer.render(template,evidence,'postgres:16@sha256:'+'c'*64,sha)
        self.assertIn('${SCM_API_ENV_FILE',out);self.assertNotIn('${SCM_API_IMAGE',out)
        with self.assertRaises(ValueError):renderer.render(template,evidence,'postgres:18@sha256:'+'c'*64,sha)

if __name__=='__main__':unittest.main()

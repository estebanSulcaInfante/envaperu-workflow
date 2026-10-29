import importlib.util
from pathlib import Path
import pytest

path = Path(__file__).parents[2] / 'deploy/personal-agent-access/prepare_config.py'
spec = importlib.util.spec_from_file_location('personal_deploy', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def base():
    return {'gateway': {'auth': {'mode': 'token', 'token': 'old'}, 'reload': {'mode': 'off'}},
            'tools': {'allow': sorted(module.SAFE_TOOLS), 'elevated': {'enabled': False}},
            'agents': {'defaults': {'model': {'primary': 'approved/model'}}, 'entries': {'scm-personal': {'workspace': '/workspace', 'agentDir': '/state/agents/scm-personal/agent'}}}}


def test_private_candidate_keeps_source_model_and_readonly_policy():
    original = base()
    output = module.prepare(original, 'https://agent.example.invalid', 'x' * 48)
    assert original['gateway']['auth']['token'] == 'old'
    assert output['gateway']['auth']['mode'] == 'trusted-proxy'
    assert 'token' not in output['gateway']['auth']
    assert output['gateway']['reload']['mode'] == 'hybrid'
    assert output['gateway']['bind'] == 'loopback'
    assert output['gateway']['roles']['definitions']['deny_all']['agents'] == []
    assert output['agents']['defaults'] == original['agents']['defaults']
    assert output['tools'] == original['tools']


@pytest.mark.parametrize('origin', ['http://agent.example.invalid', 'https://u:p@agent.example.invalid', 'https://agent.example.invalid/path', 'https://agent.example.invalid?token=x'])
def test_candidate_rejects_unsafe_origins(origin):
    with pytest.raises(ValueError):
        module.prepare(base(), origin, 'x' * 48)


@pytest.mark.parametrize('field,value', [('workspace', 'relative'), ('agentDir', None), ('agentDir', '/state/../agent'), ('agentDir', '/state//agent')])
def test_candidate_rejects_incomplete_template(field, value):
    config = base()
    config['agents']['entries']['scm-personal'][field] = value
    with pytest.raises(ValueError):
        module.prepare(config, 'https://agent.example.invalid', 'x' * 48)


def test_candidate_rejects_divergent_model():
    config = base()
    config['agents']['entries']['scm-personal']['model'] = {'primary': 'other/model'}
    with pytest.raises(ValueError):
        module.prepare(config, 'https://agent.example.invalid', 'x' * 48)

import assert from 'node:assert/strict';
import test from 'node:test';
import { loadConfig } from '../src/config.js';

test('config reuses the fixed SCM form origin for recovery without adding a return target', () => {
  const config = loadConfig({
    TESTING: 'true',
    SCM_PERSONAL_AGENT_SERVICE_SECRET: 's'.repeat(32),
    SCM_API_URL: 'http://127.0.0.1:5000/api',
    SCM_ALLOWED_ORIGIN: 'https://bridge.example.test',
    SCM_SCM_ORIGIN: 'https://scm.example.test',
    SCM_NATIVE_HTTP_ORIGIN: 'http://127.0.0.1:18789',
    SCM_NATIVE_WS_URL: 'ws://127.0.0.1:18789',
    SCM_NATIVE_ADMIN_PASSWORD_FILE: 'admin-password',
  });
  assert.equal(config.scmFormOrigin, 'https://scm.example.test');
  assert.equal(Object.hasOwn(config, 'scmRecoveryUrl'), false);
});

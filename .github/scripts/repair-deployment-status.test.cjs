const { test } = require('node:test');
const assert = require('node:assert/strict');
const { repair, raceSuccess, ENVIRONMENT, LOG_URL } = require('./repair-deployment-status.cjs');

const creator = { login: 'railway-app[bot]' };
const success = {
  id: 20, state: 'success', creator, environment: ENVIRONMENT,
  environment_url: LOG_URL, log_url: LOG_URL, description: '',
  created_at: '2026-10-07T20:15:27Z',
};
const inactive = {
  ...success, id: 22, state: 'inactive', environment_url: '',
  created_at: '2026-10-07T20:15:46Z',
};
const older = { ...success, id: 21, created_at: '2026-10-07T20:15:45Z' };

test('recognizes the recorded 7 October late-success race', () => {
  assert.equal(raceSuccess([inactive, success], [older]), success);
});

for (const [name, change] of Object.entries({
  'failed release': { state: 'failure' },
  'crashed release': { state: 'error' },
  'unfinished release': { state: 'in_progress' },
  'already successful release': { state: 'success' },
  'explicit Railway removal': { environment_url: LOG_URL },
  'manual removal': { creator: { login: 'NCorbeau' } },
  'removal with explanation': { description: 'Removed' },
  'different environment': { environment: 'preview' },
  'unrelated log URL': { log_url: 'https://example.com' },
  'invalid timestamp': { created_at: 'invalid' },
  'unrelated older success': { created_at: '2026-10-07T20:16:46Z' },
})) {
  test(`does not repair ${name}`, () => {
    assert.equal(raceSuccess([{ ...inactive, ...change }, success], [older]), null);
  });
}

test('requires immediately preceding success and a matching late success', () => {
  assert.equal(raceSuccess([inactive], [older]), null);
  assert.equal(raceSuccess([inactive, { ...success, state: 'failure' }], [older]), null);
  assert.equal(raceSuccess([inactive, success], []), null);
  assert.equal(raceSuccess([inactive, success], [{ ...older, environment: 'preview' }]), null);
  assert.equal(raceSuccess([inactive, success], [{ ...older, creator: { login: 'someone' } }]), null);
});

function fixture({ newerDeployment = false, changedStatus = false, changedMain = false,
  ready = true, explicitRemoval = false, alreadyActive = false } = {}) {
  const writes = [];
  let deploymentReads = 0;
  let currentReads = 0;
  let mainReads = 0;
  const latest = { id: 2, sha: 'current', creator, environment: ENVIRONMENT };
  const previous = { id: 1, sha: 'previous', creator, environment: ENVIRONMENT };
  const github = { rest: { repos: {
    listDeployments: async () => {
      deploymentReads++;
      return { data: newerDeployment && deploymentReads > 1
        ? [{ ...latest, id: 3 }, latest, previous] : [latest, previous] };
    },
    getBranch: async () => {
      mainReads++;
      return { data: { commit: { sha: changedMain && mainReads > 1 ? 'new-main' : 'current' } } };
    },
    listDeploymentStatuses: async ({ deployment_id }) => {
      if (deployment_id === 1) return { data: [older] };
      currentReads++;
      return { data: alreadyActive ? [success] : [
        { ...inactive,
          id: changedStatus && currentReads > 1 ? 23 : inactive.id,
          environment_url: explicitRemoval ? LOG_URL : '',
        }, success,
      ] };
    },
    createDeploymentStatus: async body => { writes.push(body); },
  } } };
  return {
    writes,
    args: {
      github, context: { repo: { owner: 'NCorbeau', repo: 'mimit' } },
      core: { info() {} },
      fetchImpl: async () => ({ ok: ready, json: async () => ({ status: 'ok' }) }),
    },
  };
}

test('restores only the newest release with auto_inactive disabled', async () => {
  const { args, writes } = fixture();
  assert.equal(await repair(args), true);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].deployment_id, 2);
  assert.equal(writes[0].state, 'success');
  assert.equal(writes[0].auto_inactive, false);
  assert.equal(writes[0].environment_url, LOG_URL);
});

for (const option of ['newerDeployment', 'changedStatus', 'changedMain', 'explicitRemoval', 'alreadyActive']) {
  test(`skips repair when ${option}`, async () => {
    const { args, writes } = fixture({ [option]: true });
    assert.equal(await repair(args), false);
    assert.deepEqual(writes, []);
  });
}

test('unhealthy API prevents metadata changes', async () => {
  const { args, writes } = fixture({ ready: false });
  await assert.rejects(repair(args), /readiness failed/);
  assert.deepEqual(writes, []);
});

test('network failure prevents metadata changes', async () => {
  const { args, writes } = fixture();
  args.fetchImpl = async () => { throw new Error('timeout'); };
  await assert.rejects(repair(args), /timeout/);
  assert.deepEqual(writes, []);
});

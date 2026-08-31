import assert from 'node:assert/strict';
import { parseArgs } from 'node:util';
import { chromium } from '@playwright/test';

const { values } = parseArgs({ options: {
  users: { type: 'string', default: '4' },
  url: { type: 'string', default: 'https://pilarprep.app' },
  'confirm-cost': { type: 'boolean', default: false },
} });
const users = Number(values.users);
assert(Number.isInteger(users) && users >= 2 && users <= 6, '--users must be between 2 and 6');
assert(values['confirm-cost'], 'This test creates one paid Nova Pro brief per fresh guest. Add --confirm-cost.');
assert.equal(new URL(values.url).protocol, 'https:', 'Live tests require HTTPS');
const timeoutMs = 720_000;
const browser = await chromium.launch({ headless: true });
const runs = [];

async function prepare(index) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(45_000);
  const run = { user: index + 1, context, page, errors: [], states: [], submissions: 0, api429: 0 };
  runs.push(run);
  page.on('pageerror', error => run.errors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/jobs')) run.submissions += 1;
  });
  await page.goto(values.url, { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: /BlueMesa Payments/ }).click();
  return run;
}

async function generate(run) {
  const started = Date.now();
  let timer;
  let listener;
  const completion = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`User ${run.user}: generation timed out`)), timeoutMs);
    listener = async response => {
      if (!new URL(response.url()).pathname.includes('/jobs')) return;
      if (response.status() === 429) run.api429 += 1;
      let body;
      try { body = await response.json(); } catch { return; }
      if (body.action !== 'brief.generate') return;
      if (body.jobId) {
        if (run.jobId && run.jobId !== body.jobId) return reject(new Error('A browser received a different job'));
        run.jobId = body.jobId;
      }
      if (body.status && !run.states.includes(body.status)) run.states.push(body.status);
      if (body.status === 'failed') return reject(new Error(`User ${run.user}: job failed (${body.errorCode || 'see scoped job status'})`));
      if (body.status === 'complete' && body.result) resolve(body.result);
    };
    run.page.on('response', listener);
  });
  completion.catch(() => {});
  try {
    await run.page.getByRole('button', { name: /^Generate AI prebrief$/ }).click();
    const result = await completion;
    assert.equal(result.provider, 'bedrock');
    assert.match(result.metadata?.modelId || '', /nova-pro/);
    assert(!result.metadata?.fallbackUsed, 'A live generation used fallback');
    assert.equal(result.metadata?.clientId, 'bluemesa-payments');
    assert.equal(run.submissions, 1, 'A single click submitted duplicate jobs');
    assert.deepEqual(run.errors, []);
    const artifactKey = result.metadata.artifactKey;
    assert.match(artifactKey, /^tenants\/guest-[^/]+\/clients\/bluemesa-payments\//);
    const tenant = artifactKey.split('/')[1];
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      const storedKey = await run.page.evaluate(() => JSON.parse(localStorage.getItem('pillarprep.workspace.v2') || '{}').generatedBrief?.metadata?.artifactKey);
      if (storedKey === artifactKey) break;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    const storedKey = await run.page.evaluate(() => JSON.parse(localStorage.getItem('pillarprep.workspace.v2') || '{}').generatedBrief?.metadata?.artifactKey);
    assert.equal(storedKey, artifactKey, 'The completed packet did not reach the correct browser workspace');
    run.summary = { user: run.user, jobId: run.jobId, model: result.metadata.modelId, elapsedMs: Date.now() - started,
      states: run.states, submissions: run.submissions, api429: run.api429 };
    run.tenant = tenant;
    console.log(JSON.stringify(run.summary));
    return run.summary;
  } finally {
    clearTimeout(timer);
    run.page.off('response', listener);
  }
}

try {
  // Separate contexts reproduce independent guest identities, not tabs sharing localStorage.
  for (let index = 0; index < users; index += 1) await prepare(index);
  const started = Date.now();
  const outcomes = await Promise.allSettled(runs.map(generate));
  const failures = outcomes.filter(outcome => outcome.status === 'rejected');
  for (const failure of failures) console.error(failure.reason.message);
  assert.equal(failures.length, 0, `${failures.length} concurrent users failed`);
  assert.equal(new Set(runs.map(run => run.tenant)).size, users, 'Guest workspace scopes collided');
  assert.equal(new Set(runs.map(run => run.jobId)).size, users, 'Guest job IDs collided');
  console.log(JSON.stringify({ publicMultiuserSmoke: 'passed', users, elapsedMs: Date.now() - started,
    isolatedWorkspaces: true, model: 'nova-pro', totalApi429: runs.reduce((sum, run) => sum + run.api429, 0) }));
} finally {
  await browser.close();
}

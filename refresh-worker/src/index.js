/**
 * Cloudflare Worker — keeps the Agile Dashboard data fresh.
 *
 * Why this exists:
 *   GitHub treats `schedule:` cron triggers as best-effort and throttles them
 *   heavily. The workflow asks for */5 but in practice fired every 1-10 hours,
 *   leaving the dashboard hours stale. This Worker runs on Cloudflare's own
 *   cron (which is reliable) and pokes GitHub's workflow_dispatch API instead.
 *
 * Why a Worker rather than a token in the browser:
 *   The GitHub PAT lives in Cloudflare's secret store, server-side. Nothing is
 *   stored in the browser, so clearing the browser cache can never break it.
 *
 * There is deliberately NO fetch handler. The dashboard is a public page, so a
 * public HTTP endpoint here would let anyone on the internet spam workflow
 * runs. Scheduled invocation only.
 */

export default {
  async scheduled(event, env, ctx) {
    // waitUntil so the Worker isn't killed before the API call completes.
    ctx.waitUntil(dispatchWorkflow(env));
  },
};

async function dispatchWorkflow(env) {
  const missing = ['GH_PAT', 'GH_OWNER', 'GH_REPO', 'GH_WORKFLOW'].filter(k => !env[k]);
  if (missing.length) {
    console.error(`Not configured — missing: ${missing.join(', ')}`);
    return;
  }

  const ref = env.GH_REF || 'main';
  const url = `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}` +
              `/actions/workflows/${env.GH_WORKFLOW}/dispatches`;

  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization':        `Bearer ${env.GH_PAT}`,
        'Accept':              'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        // GitHub rejects API requests without a User-Agent.
        'User-Agent':          'agile-dashboard-refresh-worker',
        'Content-Type':        'application/json',
      },
      body: JSON.stringify({ ref }),
    });
  } catch (err) {
    console.error(`Network error calling GitHub: ${err}`);
    return;
  }

  // Historically this endpoint returns 204 No Content; GitHub's docs now also
  // describe a 200 with the run id. Accept any 2xx so a change on their side
  // doesn't start logging false failures.
  if (res.ok) {
    console.log(`Dispatched ${env.GH_WORKFLOW} on ${ref} (HTTP ${res.status})`);
    return;
  }

  const body = await res.text().catch(() => '');
  console.error(`Dispatch failed: ${res.status} ${res.statusText} — ${body.slice(0, 300)}`);

  // The two failures worth naming, because they look identical from the dashboard
  // (data simply stops updating) but need different fixes.
  if (res.status === 401) {
    console.error('GH_PAT is invalid or expired. Regenerate it and re-run: wrangler secret put GH_PAT');
  } else if (res.status === 403) {
    console.error('GH_PAT lacks Actions write permission on this repository.');
  } else if (res.status === 404) {
    console.error(`Workflow "${env.GH_WORKFLOW}" not found on branch "${ref}", ` +
                  'or the token cannot see the repo. Note: workflow_dispatch requires ' +
                  'the workflow file to exist on the default branch.');
  }
}

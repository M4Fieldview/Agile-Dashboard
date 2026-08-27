/**
 * Cloudflare Worker — keeps the Agile Dashboard data fresh.
 *
 * Why this exists:
 *   GitHub treats `schedule:` cron triggers as best-effort and throttles them
 *   heavily. The workflow asks for a five-minute schedule but in practice fired
 *   every 1-10 hours, leaving the dashboard hours stale. This Worker runs on
 *   Cloudflare's own cron (reliable) and calls workflow_dispatch instead.
 *
 *   (Careful writing cron expressions in this comment: a slash-star sequence
 *   would close the block comment and break the file.)
 *
 * Why a Worker rather than a token in the browser:
 *   The GitHub PAT lives in Cloudflare's secret store, server-side. Nothing is
 *   stored in the browser, so clearing the browser cache can never break it.
 *
 * There is deliberately NO fetch handler. The dashboard is a public page, so a
 * public HTTP endpoint here would let anyone on the internet spam workflow
 * runs. Scheduled invocation only.
 */

// Defaults so the only thing that MUST be configured is the GH_PAT secret.
// This matters when deploying from the Cloudflare dashboard by hand: one
// secret to set instead of five variables to type correctly. Each can still
// be overridden with a plain-text variable of the same name.
const DEFAULTS = {
  GH_OWNER:    'M4Fieldview',
  GH_REPO:     'Agile-Dashboard',
  GH_WORKFLOW: 'fetch-jira-data.yml',
  GH_REF:      'main',
};

export default {
  async scheduled(event, env, ctx) {
    // waitUntil so the Worker isn't killed before the API call completes.
    ctx.waitUntil(dispatchWorkflow(env));
  },
};

async function dispatchWorkflow(env) {
  if (!env.GH_PAT) {
    console.error('Not configured — the GH_PAT secret is missing.');
    return;
  }

  const owner    = env.GH_OWNER    || DEFAULTS.GH_OWNER;
  const repo     = env.GH_REPO     || DEFAULTS.GH_REPO;
  const workflow = env.GH_WORKFLOW || DEFAULTS.GH_WORKFLOW;
  const ref      = env.GH_REF      || DEFAULTS.GH_REF;
  const url = `https://api.github.com/repos/${owner}/${repo}` +
              `/actions/workflows/${workflow}/dispatches`;

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
    console.log(`Dispatched ${workflow} on ${ref} (HTTP ${res.status})`);
    return;
  }

  const body = await res.text().catch(() => '');
  console.error(`Dispatch failed: ${res.status} ${res.statusText} — ${body.slice(0, 300)}`);

  // The two failures worth naming, because they look identical from the dashboard
  // (data simply stops updating) but need different fixes.
  if (res.status === 401) {
    console.error('GH_PAT is invalid or expired. Replace the GH_PAT secret.');
  } else if (res.status === 403) {
    console.error('GH_PAT lacks Actions write permission on this repository, ' +
                  'or an organization-owned fine-grained token is still pending approval.');
  } else if (res.status === 404) {
    console.error(`Workflow "${workflow}" not found on branch "${ref}", ` +
                  'or the token cannot see the repo. Note: workflow_dispatch requires ' +
                  'the workflow file to exist on the default branch.');
  }
}

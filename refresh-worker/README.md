# Dashboard refresh worker

Keeps `data.json` fresh by triggering the **Refresh Jira Data** workflow every
5 minutes from Cloudflare's cron instead of GitHub's.

## Why

GitHub's `schedule:` trigger is best-effort and gets throttled. The workflow
asks for `*/5` but measured actual firing was every **1–10 hours**:

```
14:26 → 15:59 → 16:53 → 18:51 → 21:19 → 00:52 → 10:46
```

Every run succeeded — they simply weren't being started. Cloudflare's cron is
reliable, so this Worker calls `workflow_dispatch` on schedule.

The GitHub token lives in Cloudflare's secret store (server-side). Nothing is
stored in the browser, so clearing the browser cache cannot break it. The
workflow's own `schedule:` trigger is left in place as a slow fallback.

## Setup A — from the Cloudflare dashboard (no Node.js needed)

Use this if `node`/`npx` aren't installed. Everything happens in the browser.

1. Create the GitHub token (see **Token** below).
2. https://dash.cloudflare.com → **Workers & Pages** → **Create** → **Create Worker**.
3. Name it `agile-dashboard-refresh` → **Deploy** (the placeholder code is fine for now).
4. **Edit code** → delete the placeholder → paste all of [`src/index.js`](src/index.js) → **Deploy**.
5. **Settings** → **Variables and Secrets** → **Add**:
   - Type **Secret**, name `GH_PAT`, value = your token → **Deploy**.
   - No other variables are needed; owner/repo/workflow/branch have defaults
     baked into the code. Add plain-text variables of the same name only if you
     want to override them.
6. **Settings** → **Triggers** → **Cron Triggers** → **Add Cron Trigger** →
   `*/5 * * * *` → save.

Verify: within ~5 minutes a run with event `workflow_dispatch` should appear at
https://github.com/M4Fieldview/Agile-Dashboard/actions — and the dashboard's
age should stop climbing past ~5 minutes. The Worker's **Logs** tab shows
`Dispatched fetch-jira-data.yml on main` on success.

## Setup B — from the command line

Requires Node.js and a free Cloudflare account. Run from this directory.

### 1. Create a GitHub token

See **Token** below, then continue.

### 2. Deploy

```bash
npx wrangler login
```

```bash
npx wrangler secret put GH_PAT
```

Paste the token when prompted. It is write-only from then on — Cloudflare will
not show it back to you.

```bash
npx wrangler deploy
```

### 3. Verify

Trigger it once by hand rather than waiting for the cron:

```bash
npx wrangler dev --test-scheduled
```

then in another terminal:

```bash
curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+*"
```

You should see `Dispatched fetch-jira-data.yml on main` in the dev output, and a
new `workflow_dispatch` run in the repo's Actions tab within a few seconds.

To watch the deployed Worker's live logs:

```bash
npx wrangler tail
```

## Token

Fine-grained token at https://github.com/settings/personal-access-tokens/new

- **Resource owner** → `M4Fieldview`
- **Repository access** → **Only select repositories** → `Agile-Dashboard`.
  Do *not* pick "Public repositories" — that is read-only and cannot dispatch
  a workflow.
- **Repository permissions** → **Actions: Read and write**. That is the only
  one needed; `Metadata: Read-only` is added automatically.
- **Expiration** → your call. If it expires the dashboard silently stops
  updating, so either set a calendar reminder or choose no expiration.

Because the resource owner is an organization, the token may come back
**pending approval** — an org owner has to approve it under
Organization settings → Personal access tokens. A classic token with the `repo`
scope works immediately and skips that, at the cost of broader access.

## Repo growth — read this before lowering the interval

`data.json` is ~1.25 MB. Git compresses each revision to roughly 7 KB, so a
5-minute cadence adds on the order of **1–2 MB per day** of permanent history:

| Interval | Commits/day | Rough growth/year |
|---|---|---|
| 5 min | 288 | ~350–700 MB |
| 10 min | 144 | ~180–350 MB |
| 15 min | 96 | ~120–230 MB |

GitHub's soft repo limit is 1 GB. Two ways to avoid the problem entirely:

1. **Raise the interval** in `wrangler.toml` — simplest.
2. **Stop committing `data.json`.** Switch GitHub Pages to deploy from an
   Actions artifact (`actions/upload-pages-artifact` + `actions/deploy-pages`)
   instead of committing to `main`. Pages deployments don't accumulate in git
   history, so any cadence becomes free. This is the better long-term fix but
   requires changing Settings → Pages → Source to "GitHub Actions".

## Cost

Free tier: 100,000 Worker requests/day and cron triggers included. This uses
288/day. The repo is public, so GitHub Actions minutes are unlimited and free.

## Troubleshooting

Symptom is always the same — the dashboard's age stops advancing. Run
`npx wrangler tail` and look for:

| Log | Meaning | Fix |
|---|---|---|
| `401` | Token invalid or expired | `npx wrangler secret put GH_PAT` with a new token |
| `403` | Token lacks Actions write | Re-issue with **Actions: Read and write** |
| `404` | Workflow or repo not visible to the token | Check `GH_OWNER`/`GH_REPO`/`GH_WORKFLOW`; the workflow must exist on the default branch |
| nothing at all | Cron not firing | Confirm the deploy: `npx wrangler deployments list` |

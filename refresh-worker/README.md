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

## Setup

Requires a free Cloudflare account. Run everything from this directory.

### 1. Create a GitHub token

Fine-grained token at https://github.com/settings/personal-access-tokens/new

- **Repository access** → Only select repositories → `M4Fieldview/Agile-Dashboard`
- **Repository permissions** → **Actions: Read and write** (this is the only one needed)
- **Expiration** → as long as you're comfortable with. If it expires the
  dashboard silently stops updating, so set a calendar reminder, or use "No
  expiration" and accept the tradeoff.

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

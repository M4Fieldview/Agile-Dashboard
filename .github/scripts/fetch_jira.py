#!/usr/bin/env python3
"""
Fetches Jira sprint data and writes data.json for the Agile Dashboard.

Required environment variables:
  JIRA_URL   — e.g. https://yourcompany.atlassian.net
  JIRA_EMAIL — your Jira account email
  JIRA_TOKEN — Jira API token
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────
JIRA_URL   = os.environ.get('JIRA_URL',   '').rstrip('/')
JIRA_EMAIL = os.environ.get('JIRA_EMAIL', '')
JIRA_TOKEN = os.environ.get('JIRA_TOKEN', '')

if not all([JIRA_URL, JIRA_EMAIL, JIRA_TOKEN]):
    print('ERROR: JIRA_URL, JIRA_EMAIL, and JIRA_TOKEN must all be set.', file=sys.stderr)
    sys.exit(1)

_AUTH = 'Basic ' + base64.b64encode(f'{JIRA_EMAIL}:{JIRA_TOKEN}'.encode()).decode()

ISSUE_FIELDS = ','.join([
    'summary', 'assignee', 'issuetype', 'status', 'components',
    'timespent', 'timeoriginalestimate', 'timeestimate',
    'priority', 'customfield_10016',
])

# ── HTTP helper ─────────────────────────────────────────────────────────────
def get(path, params=None):
    url = JIRA_URL + path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        url += '?' + urllib.parse.urlencode(filtered)
    req = urllib.request.Request(url, headers={
        'Authorization': _AUTH,
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:400]
        raise RuntimeError(f'HTTP {e.code} GET {path}: {body}')


def get_all(path, extra=None):
    """Paginate through all results, returning a flat list."""
    results, start = [], 0
    while True:
        params = {**(extra or {}), 'startAt': start, 'maxResults': 100}
        data = get(path, params)
        items = (
            data.get('values') or
            data.get('issues') or
            data.get('worklogs') or
            []
        )
        results.extend(items)
        total = data.get('total', len(items))
        start += len(items)
        if not items or start >= total:
            break
    return results


# ── Data slimming ────────────────────────────────────────────────────────────
def slim_issue(issue):
    """Return only the fields the dashboard needs to keep data.json small."""
    f = issue.get('fields', {})
    assignee = f.get('assignee') or {}
    issuetype = f.get('issuetype') or {}
    status = f.get('status') or {}
    status_cat = status.get('statusCategory') or {}

    return {
        'key': issue['key'],
        'fields': {
            'summary':               f.get('summary', ''),
            'timespent':             f.get('timespent') or 0,
            'timeoriginalestimate':  f.get('timeoriginalestimate') or 0,
            'timeestimate':          f.get('timeestimate') or 0,
            'assignee': {
                'displayName': assignee.get('displayName', 'Unassigned'),
                'avatarUrls':  assignee.get('avatarUrls', {}),
                'emailAddress': assignee.get('emailAddress', ''),
            } if assignee else None,
            'issuetype': {
                'name':    issuetype.get('name', 'Unknown'),
                'iconUrl': issuetype.get('iconUrl', ''),
            },
            'status': {
                'name': status.get('name', ''),
                'statusCategory': {'key': status_cat.get('key', 'new')},
            },
            'components': [{'name': c['name']} for c in f.get('components', [])],
        },
        'worklogs': slim_worklogs(issue.get('worklogs', [])),
    }


def slim_worklogs(worklogs):
    """Strip worklogs down to only the fields used in charts."""
    out = []
    for wl in worklogs:
        author = wl.get('author') or {}
        out.append({
            'author': {
                'displayName':  author.get('displayName', 'Unknown'),
                'avatarUrls':   author.get('avatarUrls', {}),
                'emailAddress': author.get('emailAddress', ''),
            },
            'started':          wl.get('started', ''),
            'timeSpentSeconds': wl.get('timeSpentSeconds', 0),
        })
    return out


# ── Fetchers ─────────────────────────────────────────────────────────────────
def fetch_worklogs(issue_key):
    try:
        data = get(f'/rest/api/3/issue/{issue_key}/worklog')
        return data.get('worklogs', [])
    except Exception as e:
        print(f'    Warning: worklogs for {issue_key}: {e}', file=sys.stderr)
        return []


def fetch_sprint_data(sprint_id, sprint_name):
    print(f'    Fetching issues…', file=sys.stderr)
    issues = get_all(
        f'/rest/agile/1.0/sprint/{sprint_id}/issue',
        {'fields': ISSUE_FIELDS},
    )
    print(f'    {len(issues)} issues — fetching worklogs…', file=sys.stderr)
    for i, issue in enumerate(issues):
        issue['worklogs'] = fetch_worklogs(issue['key'])
        if (i + 1) % 10 == 0:
            print(f'    {i + 1}/{len(issues)} worklogs fetched…', file=sys.stderr)
    return [slim_issue(i) for i in issues]


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print('Fetching boards…', file=sys.stderr)
    boards_resp = get('/rest/agile/1.0/board', {'maxResults': 50})
    boards = boards_resp.get('values', [])
    print(f'Found {len(boards)} board(s)', file=sys.stderr)

    output_boards = []

    for board in boards:
        board_id   = board['id']
        board_name = board['name']
        board_type = board.get('type', 'unknown')
        print(f'\nBoard: {board_name} ({board_type}, id={board_id})', file=sys.stderr)

        # Fetch active sprint + last 3 closed sprints
        sprints = []
        for state, limit in [('active', 2), ('closed', 3), ('future', 1)]:
            try:
                data = get(
                    f'/rest/agile/1.0/board/{board_id}/sprint',
                    {'state': state, 'maxResults': limit},
                )
                sprints.extend(data.get('values', []))
            except Exception as e:
                print(f'  Warning fetching {state} sprints: {e}', file=sys.stderr)

        if not sprints:
            print('  No sprints found, skipping.', file=sys.stderr)
            continue

        output_sprints = []
        for sprint in sprints:
            sprint_id   = sprint['id']
            sprint_name = sprint['name']
            sprint_state = sprint.get('state', '?')
            print(f'  Sprint: {sprint_name} ({sprint_state})', file=sys.stderr)
            try:
                issues = fetch_sprint_data(sprint_id, sprint_name)
                output_sprints.append({
                    'id':        sprint_id,
                    'name':      sprint_name,
                    'state':     sprint_state,
                    'startDate': sprint.get('startDate'),
                    'endDate':   sprint.get('endDate'),
                    'goal':      sprint.get('goal', ''),
                    'issues':    issues,
                })
            except Exception as e:
                print(f'  Error on sprint {sprint_name}: {e}', file=sys.stderr)

        output_boards.append({
            'id':      board_id,
            'name':    board_name,
            'type':    board_type,
            'sprints': output_sprints,
        })

    result = {
        'fetchedAt': datetime.now(timezone.utc).isoformat(),
        'boards':    output_boards,
    }

    out_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data.json')
    out_path = os.path.normpath(out_path)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, separators=(',', ':'))

    size_kb = os.path.getsize(out_path) / 1024
    print(f'\nWrote {out_path} ({size_kb:.1f} KB)', file=sys.stderr)
    print(f'Boards: {len(output_boards)}, '
          f'Sprints: {sum(len(b["sprints"]) for b in output_boards)}', file=sys.stderr)


if __name__ == '__main__':
    main()

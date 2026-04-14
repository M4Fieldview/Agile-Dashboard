#!/usr/bin/env python3
"""
Fetches Jira data and writes data.json for the Agile Dashboard.

Two-pass discovery:
  1. Agile boards API  — finds all scrum/kanban boards (company-managed projects)
  2. Projects API      — finds team-managed (next-gen) projects not covered by boards,
                         and groups their recent issues into rolling 2-week windows

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
from datetime import datetime, timedelta, timezone

# ── Config ───────────────────────────────────────────────────────────────────
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

# How far back to look for worklogs in non-sprint (kanban/project) boards
RECENT_DAYS = 90

# ── HTTP helpers ─────────────────────────────────────────────────────────────
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


def get_all(path, extra=None, item_key=None):
    """Paginate through all results, returning a flat list."""
    results, start = [], 0
    while True:
        params = {**(extra or {}), 'startAt': start, 'maxResults': 100}
        data = get(path, params)
        if item_key:
            items = data.get(item_key, [])
        else:
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


# ── Data slimming ─────────────────────────────────────────────────────────────
def slim_issue(issue):
    f          = issue.get('fields', {})
    assignee   = f.get('assignee') or {}
    issuetype  = f.get('issuetype') or {}
    status     = f.get('status') or {}
    status_cat = status.get('statusCategory') or {}

    return {
        'key': issue['key'],
        'fields': {
            'summary':              f.get('summary', ''),
            'timespent':            f.get('timespent') or 0,
            'timeoriginalestimate': f.get('timeoriginalestimate') or 0,
            'timeestimate':         f.get('timeestimate') or 0,
            'assignee': {
                'displayName':  assignee.get('displayName', 'Unassigned'),
                'avatarUrls':   assignee.get('avatarUrls', {}),
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


# ── Fetchers ──────────────────────────────────────────────────────────────────
def fetch_worklogs(issue_key):
    try:
        data = get(f'/rest/api/3/issue/{issue_key}/worklog')
        return data.get('worklogs', [])
    except Exception as e:
        print(f'      Warning: worklogs for {issue_key}: {e}', file=sys.stderr)
        return []


def attach_worklogs(issues):
    for i, issue in enumerate(issues):
        issue['worklogs'] = fetch_worklogs(issue['key'])
        if (i + 1) % 10 == 0:
            print(f'      {i + 1}/{len(issues)} worklogs done…', file=sys.stderr)
    return issues


def fetch_sprint_issues(sprint_id):
    issues = get_all(
        f'/rest/agile/1.0/sprint/{sprint_id}/issue',
        {'fields': ISSUE_FIELDS},
        item_key='issues',
    )
    return [slim_issue(i) for i in attach_worklogs(issues)]


def fetch_issues_by_jql(jql):
    """Fetch issues matching JQL and return slimmed list."""
    issues = get_all(
        '/rest/api/3/search',
        {'jql': jql, 'fields': ISSUE_FIELDS},
        item_key='issues',
    )
    return [slim_issue(i) for i in attach_worklogs(issues)]


# ── Board-based fetch (company-managed scrum/kanban) ─────────────────────────
def process_board(board):
    board_id   = board['id']
    board_name = board['name']
    board_type = board.get('type', 'unknown')
    print(f'\nBoard: {board_name} ({board_type}, id={board_id})', file=sys.stderr)

    if board_type == 'kanban':
        return process_kanban_board(board_id, board_name, board_type)
    else:
        return process_scrum_board(board_id, board_name, board_type)


def process_scrum_board(board_id, board_name, board_type):
    sprints = []
    for state, limit in [('active', 2), ('closed', 4), ('future', 1)]:
        try:
            data = get(
                f'/rest/agile/1.0/board/{board_id}/sprint',
                {'state': state, 'maxResults': limit},
            )
            sprints.extend(data.get('values', []))
        except Exception as e:
            print(f'  Warning fetching {state} sprints: {e}', file=sys.stderr)

    if not sprints:
        print('  No sprints — skipping.', file=sys.stderr)
        return None

    output_sprints = []
    for sprint in sprints:
        sprint_id    = sprint['id']
        sprint_name  = sprint['name']
        sprint_state = sprint.get('state', '?')
        print(f'  Sprint: {sprint_name} ({sprint_state})', file=sys.stderr)
        try:
            issues = fetch_sprint_issues(sprint_id)
            print(f'    {len(issues)} issues', file=sys.stderr)
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
            print(f'  Error: {e}', file=sys.stderr)

    return {'id': board_id, 'name': board_name, 'type': board_type, 'sprints': output_sprints}


def process_kanban_board(board_id, board_name, board_type):
    """
    Kanban boards have no sprints.
    Fetch recently-updated board issues, attach worklogs, then bucket
    into rolling 2-week windows by worklog date.
    The board is always emitted (even if empty) so it appears in the UI.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime('%Y-%m-%d')
    # Use 'updated' not 'worklogDate' — catches issues even if no worklogs logged yet
    print(f'  Fetching kanban issues (updated >= {cutoff})…', file=sys.stderr)

    try:
        issues = get_all(
            f'/rest/agile/1.0/board/{board_id}/issue',
            {
                'jql':    f'updated >= "{cutoff}"',
                'fields': ISSUE_FIELDS,
            },
            item_key='issues',
        )
    except Exception as e:
        print(f'  Error fetching kanban issues: {e}', file=sys.stderr)
        # Still emit the board with an empty window so it shows in the UI
        return _empty_board(board_id, board_name, board_type)

    print(f'  {len(issues)} recently updated issues — attaching worklogs…', file=sys.stderr)
    attach_worklogs(issues)

    # Bucket into rolling 2-week windows by worklog start date
    windows = build_time_windows(RECENT_DAYS)
    output_sprints = []
    for label, start_d, end_d in windows:
        window_issues = [
            slim_issue(i) for i in issues
            if any(
                start_d <= wl.get('started', '')[:10] <= end_d
                for wl in (i.get('worklogs') or [])
            )
        ]
        # Include window even if empty so the full date range is browsable
        output_sprints.append({
            'id':        f'{board_id}_{start_d}',
            'name':      label,
            'state':     'active' if label == windows[0][0] else 'closed',
            'startDate': start_d + 'T00:00:00.000Z',
            'endDate':   end_d   + 'T23:59:59.000Z',
            'goal':      '',
            'issues':    window_issues,
        })

    with_data = sum(1 for s in output_sprints if s['issues'])
    print(f'  {len(output_sprints)} windows ({with_data} with worklog data).', file=sys.stderr)
    return {'id': board_id, 'name': board_name, 'type': board_type, 'sprints': output_sprints}


def _empty_board(board_id, board_name, board_type):
    """Return a board entry with a single empty window (board visible in UI, no data)."""
    windows = build_time_windows(RECENT_DAYS)
    label, start_d, end_d = windows[0]
    return {
        'id':   board_id,
        'name': board_name,
        'type': board_type,
        'sprints': [{
            'id': f'{board_id}_{start_d}', 'name': label, 'state': 'active',
            'startDate': start_d + 'T00:00:00.000Z',
            'endDate':   end_d   + 'T23:59:59.000Z',
            'goal': '', 'issues': [],
        }],
    }


# ── Project-based fetch (team-managed / next-gen projects) ───────────────────
def fetch_all_projects():
    """Return all Jira projects the user can see."""
    return get_all('/rest/api/3/project/search', item_key='values')


def get_board_project_keys(boards):
    """Collect all project keys already covered by Agile boards."""
    keys = set()
    for board in boards:
        loc = board.get('location') or {}
        pk = loc.get('projectKey') or loc.get('projectId')
        if pk:
            keys.add(str(pk))
    return keys


def process_project_as_board(project, covered_board_ids):
    """Create a virtual board for a project not covered by an Agile board."""
    proj_key  = project['key']
    proj_name = project['name']
    proj_type = project.get('projectTypeKey', 'software')
    print(f'\nProject (no board): {proj_name} ({proj_key})', file=sys.stderr)

    windows = build_time_windows(RECENT_DAYS)
    output_sprints = []

    for label, start_d, end_d in windows:
        jql = (
            f'project = "{proj_key}" '
            f'AND worklogDate >= "{start_d}" AND worklogDate <= "{end_d}" '
            f'ORDER BY updated DESC'
        )
        print(f'  Window: {label}', file=sys.stderr)
        try:
            issues = fetch_issues_by_jql(jql)
            if issues:
                output_sprints.append({
                    'id':        f'{proj_key}_{start_d}',
                    'name':      label,
                    'state':     'active' if label == windows[0][0] else 'closed',
                    'startDate': start_d + 'T00:00:00.000Z',
                    'endDate':   end_d   + 'T23:59:59.000Z',
                    'goal':      '',
                    'issues':    issues,
                })
        except Exception as e:
            print(f'    Error: {e}', file=sys.stderr)

    if not output_sprints:
        print('  No recent worklogs — skipping.', file=sys.stderr)
        return None

    return {
        'id':      proj_key,
        'name':    proj_name,
        'type':    f'project ({proj_type})',
        'sprints': output_sprints,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────
def build_time_windows(total_days, window_days=14):
    """Build non-overlapping 2-week windows covering the past N days, newest first."""
    now     = datetime.now(timezone.utc).date()
    windows = []
    end     = now
    while (now - end).days < total_days:
        start = end - timedelta(days=window_days - 1)
        if start < now - timedelta(days=total_days):
            start = now - timedelta(days=total_days)
        label = f'{start.strftime("%b %d")} – {end.strftime("%b %d, %Y")}'
        windows.append((label, start.isoformat(), end.isoformat()))
        end = start - timedelta(days=1)
        if len(windows) >= 4:
            break
    return windows


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # ── Pass 1: Agile boards ─────────────────────────────────────────────────
    print('=== Pass 1: Agile boards ===', file=sys.stderr)
    all_boards = get_all('/rest/agile/1.0/board')
    print(f'Found {len(all_boards)} board(s) via Agile API', file=sys.stderr)
    for b in all_boards:
        loc = b.get('location') or {}
        print(f'  [{b.get("type","?")}] id={b["id"]} name="{b["name"]}" '
              f'project={loc.get("projectKey","?")}', file=sys.stderr)

    output_boards = []
    seen_project_keys = set()

    for board in all_boards:
        result = process_board(board)
        if result:
            output_boards.append(result)
            # Track which project keys are covered
            loc = board.get('location') or {}
            pk  = loc.get('projectKey') or loc.get('projectId')
            if pk:
                seen_project_keys.add(str(pk))

    # ── Pass 2: All projects with recent worklogs (single JQL sweep) ────────────
    print('\n=== Pass 2: JQL sweep for all recently active projects ===', file=sys.stderr)
    try:
        # Collect project keys already fully covered by scrum boards
        # (kanban boards don't have sprint data so we still want their project issues)
        scrum_project_keys = set()
        for b in output_boards:
            if b['type'] == 'scrum':
                loc = next((raw.get('location', {}) for raw in all_boards if raw['id'] == b['id']), {})
                pk = loc.get('projectKey') or loc.get('projectId')
                if pk:
                    scrum_project_keys.add(str(pk))

        cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime('%Y-%m-%d')
        jql = f'worklogDate >= "{cutoff}" ORDER BY project ASC, key ASC'
        print(f'JQL: {jql}', file=sys.stderr)

        all_recent_issues = get_all('/rest/api/3/search', {'jql': jql, 'fields': ISSUE_FIELDS}, item_key='issues')
        print(f'Found {len(all_recent_issues)} issues with recent worklogs across all projects', file=sys.stderr)

        # Group issues by project key
        by_project = {}
        for issue in all_recent_issues:
            pk = issue['key'].split('-')[0]
            if pk not in scrum_project_keys:
                by_project.setdefault(pk, []).append(issue)

        print(f'Projects NOT already covered by scrum boards: {list(by_project.keys())}', file=sys.stderr)

        for proj_key, issues in by_project.items():
            print(f'\nProject: {proj_key} ({len(issues)} issues with recent worklogs)', file=sys.stderr)
            # Attach worklogs
            attach_worklogs(issues)
            # Bucket into 2-week windows
            windows = build_time_windows(RECENT_DAYS)
            output_sprints = []
            for label, start_d, end_d in windows:
                window_issues = [
                    slim_issue(i) for i in issues
                    if any(
                        start_d <= wl.get('started', '')[:10] <= end_d
                        for wl in (i.get('worklogs') or [])
                    )
                ]
                if window_issues:
                    output_sprints.append({
                        'id':        f'{proj_key}_{start_d}',
                        'name':      label,
                        'state':     'active' if label == windows[0][0] else 'closed',
                        'startDate': start_d + 'T00:00:00.000Z',
                        'endDate':   end_d   + 'T23:59:59.000Z',
                        'goal':      '',
                        'issues':    window_issues,
                    })
            if output_sprints:
                output_boards.append({
                    'id':      proj_key,
                    'name':    proj_key,  # will be enriched below
                    'type':    'project',
                    'sprints': output_sprints,
                })

        # Enrich project names from the projects API
        try:
            all_projects = fetch_all_projects()
            proj_name_map = {p['key']: p['name'] for p in all_projects}
            for board in output_boards:
                if board['type'] == 'project' and board['id'] in proj_name_map:
                    board['name'] = proj_name_map[board['id']]
        except Exception as e:
            print(f'Warning: could not enrich project names: {e}', file=sys.stderr)

    except Exception as e:
        print(f'Warning: JQL sweep failed: {e}', file=sys.stderr)

    # ── Write output ──────────────────────────────────────────────────────────
    result = {
        'fetchedAt': datetime.now(timezone.utc).isoformat(),
        'boards':    output_boards,
    }

    out_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), '..', '..', 'data.json')
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, separators=(',', ':'))

    size_kb = os.path.getsize(out_path) / 1024
    total_sprints = sum(len(b['sprints']) for b in output_boards)
    print(f'\nWrote {out_path} ({size_kb:.1f} KB)', file=sys.stderr)
    print(f'Boards/projects: {len(output_boards)}, Sprint windows: {total_sprints}', file=sys.stderr)


if __name__ == '__main__':
    main()

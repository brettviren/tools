# gitea-mirror

`gitea-mirror` keeps a Gitea account (e.g. `bvgh`) populated with pull-mirrors
of a selected subset of GitHub repositories.  It discovers GitHub repos from a
configured set of users/orgs, decides per repo whether it *should* be mirrored
(by rules, overridable by hand), and reconciles Gitea to match.  It is meant to
run unattended from cron (`sync`) and interactively for review and toggling.

## Plan

### Problem

- Hundreds of GitHub repos across a personal account and several orgs; new ones
  appear often.
- Not everything should be mirrored: drive-by PR forks, most repos of big orgs.
- Some forks and some org repos *should* be mirrored.
- Decisions must be recorded, reviewable and reversible, whether made by a rule
  or by hand.  Turning a mirror off must not silently destroy the Gitea copy.

### Model

Each GitHub repo seen is a **record** in a local state file, keyed by its
GitHub `owner/name`.  A record holds:

| field | meaning |
|---|---|
| `meta` | last-seen GitHub facts: fork, private, archived, description, topics, language, timestamps |
| `rule` | the result of the latest rule evaluation: `{name, action}` |
| `manual` | an optional hand-made override: `{action, at, note}` |
| `on_unmirror` | optional per-repo `leave`/`prune` override |
| `gitea_name` | optional per-repo Gitea repo name override |
| `gitea` | what is known to exist on Gitea: name, when, how (`auto`, `manual`, `adopted`) |
| `gone` | the repo no longer appears in any source |

The **decision** of a record is `manual.action` if set, else `rule.action`.
Actions are `mirror`, `skip` and `review` (undecided, waiting for a human).
Clearing a manual override (`set --rules`) hands the decision back to the rules,
which makes every hand decision reversible.  Re-running rules after editing the
config may flip rule decisions; `scan` reports every flip.

The **reconcile plan** compares decision with Gitea reality:

| decision | on Gitea? | plan |
|---|---|---|
| mirror | no | `create` |
| mirror | yes | nothing |
| skip | yes | `prune` if `on_unmirror == prune`, else `leave` (reported only) |
| skip | no | nothing |
| review | – | nothing; listed as *pending* |

`on_unmirror` defaults to `leave` (config `defaults.on_unmirror`), so no rule
edit can delete Gitea data unless you opt in globally or per repo.

Existing Gitea mirrors are matched to GitHub repos by their `original_url`, so
the current 183 mirrors are **adopted** on the first scan rather than recreated.
Gitea mirrors whose origin is not in any configured source are reported as
*untracked* and never touched.

Every change of decision, creation, deletion or failure is appended to a
JSON-lines history log so you can see what changed and when.

### Rules

Rules live in the config as an ordered list; the first rule whose conditions
all match wins.  If none match, the source's `default` applies (itself
defaulting to `review`).  Conditions: `source`, `owner` (glob), `repo` (glob or
list of globs on the repo name), `regex` (on `owner/name`), `fork`, `private`, `archived`, `topic`, `language`,
`description` (regex).  A rule with action `mirror` creates mirrors
unattended; anything you are unsure of should be `review` so it lands in the
pending queue.

### Credentials

Nothing secret is stored in the config.  Each service gets a credential spec
resolved in this order of preference:

1. `token_command` — a command printing the token on stdout
   (e.g. `pass show gitea/bvgh-token`, `gh auth token`).
2. `token_env` — an environment variable name.
3. `token_file` — a mode-0600 file (what `auth login` writes if you don't
   use `pass`).
4. Gitea only: `user` + `password_command` for HTTP basic auth.

`auth login github|gitea` opens the browser on the right token page (for
GitHub, a classic-token form pre-filled with name and scopes), reads the
pasted token with hidden input, verifies it against the API, and stores it via
a configured `store_command` (e.g. `pass insert -m gitea/bvgh-token`) or in the
0600 token file.

To mirror *private* GitHub repos, Gitea itself needs a GitHub token; the
GitHub credential (or a separate `mirror_token` credential) is handed to Gitea
at mirror creation.

### Architecture

`src/tools/gitea_mirror/`:

| module | role |
|---|---|
| `__main__.py` | Click commands; thin compositions of library calls |
| `config.py` | load/validate TOML config, starter template |
| `creds.py` | credential resolution, browser token workflow |
| `rest.py` | tiny stdlib JSON REST client with Link-header pagination |
| `github.py` | list repos of user/org sources, fetch one repo |
| `gitea.py` | list owner repos, create mirror (`/repos/migrate`), delete |
| `rules.py` | rule matching and evaluation |
| `state.py` | records, decisions, locked atomic JSON store, history log |
| `core.py` | scan (discover+adopt+evaluate), plan, apply |
| `report.py` | tables / JSON rendering |
| `review.py` | interactive review loop |

The two HTTP clients are plain classes so tests substitute fakes; no network
is needed for the test suite.  State writes are atomic (write+rename) and
guarded by a `flock` so a cron `sync` and an interactive `review` cannot
corrupt each other.

### What a mirror carries

A Gitea pull mirror carries git data only: branches, tags and commits (plus
LFS objects if `lfs = true`, and the wiki if `wiki = true`).  Gitea refuses to
copy issues, pull requests, comments, labels, milestones or releases into a
mirror: its migrate API turns those options off whenever `mirror` is set.  So
gitea-mirror can't add issues to a mirror; they would need a separate archive.

### Creating mirrors politely

Gitea's `github` migrator queries the GitHub API.  Without a token those
calls count against GitHub's anonymous limit of 60 requests/hour for the
Gitea server's IP, and a bulk run soon fails with `HTTP 422: Remote visit
addressed rate limitation`.  So by default (`gitea.service = "git"`) public
repos are mirrored with a plain git clone, which uses no API calls and stores
no token in Gitea.  Private repos always use the `github` migrator with the
GitHub token (5000 requests/hour).

In addition, `apply`/`sync`:

- space creations by `create_delay` seconds (`--delay`);
- cap creations per run at `max_creates` (`--max`), handy for spreading a
  backlog over several cron runs;
- stop at the first remote rate-limit error instead of failing every
  remaining repo;
- on Ctrl-C, stop and keep the record of everything already done.

A failed Gitea migration leaves an empty, non-mirror repo behind, so a retry
gets `HTTP 409: already exists`.  On a 409, gitea-mirror adopts the repo if it
is already a mirror of the same origin, deletes and recreates it if it is an
empty stub with the same origin, and otherwise reports the conflict.  `scan`
lists such stubs.

Some older Gitea pull mirrors have no `original_url` in the API (the web page
still shows "mirror of …", which comes from the mirror's git remote).  Those
can't be matched automatically and are listed as `untracked`.  For such a
conflict, choose per repo:

```
gitea-mirror set gegede --adopt-existing           # it is the right mirror; keep it
gitea-mirror set gegede --replace-existing         # delete it and mirror afresh (asks first)
gitea-mirror set gegede --gitea-name gegede2 --apply   # keep it, mirror alongside
```

`--adopt-existing` only accepts a pull mirror whose origin matches or is not
recorded; it refuses regular repos and mirrors of something else.

### Commands

```
gitea-mirror [-c CONFIG] [-L LEVEL] <command> ...

init                 write a starter config
auth login SERVICE   browser-assisted token acquisition (github|gitea)
auth check           verify both credentials and Gitea owner access
scan                 discover + adopt + evaluate rules; no Gitea writes
sync                 scan then apply (cron entry point)
apply                apply the plan from current state without rescanning
status               counts per category
list                 records filtered by category/source/pattern
show REPO            one record, its rule match and history
review               interactively decide pending (or any category of) repos
set PATTERN...       manual decision / prune policy / gitea name
add OWNER/NAME       mirror a repo outside the configured sources
history              show the event log
```

### Steps

1. Config + state + rules, with unit tests (pure, no network).
2. REST client, GitHub and Gitea clients.
3. Core scan/plan/apply against fake clients, with tests.
4. Credentials and `auth` workflow.
5. CLI commands, report rendering, interactive review; CLI tests.
6. Register in `pyproject.toml`, document usage (below).

### Later

- Notification: `sync` writes `last-report.json` in the state directory and
  exits non-zero-free; a notifier can consume that file or wrap `sync --json`.
- Optional fork-parent lookups (e.g. "forks of org X").

## Usage

### Setup

```
$ gitea-mirror init                  # writes ~/.config/gitea-mirror/config.toml
$ $EDITOR ~/.config/gitea-mirror/config.toml
$ gitea-mirror auth login gitea      # or set token_command to a pass entry
$ gitea-mirror auth login github     # or token_command = "gh auth token"
$ gitea-mirror auth check
$ gitea-mirror scan                  # adopts existing mirrors, queues new ones
$ gitea-mirror status
```

Before the first `sync`, check what it would do:

```
$ gitea-mirror apply -n
```

### Cron

```
17 * * * *  gitea-mirror sync -q
```

`-q` prints only when something happened (new pending repos, mirrors created
or pruned, failures), so cron mail stays quiet otherwise.

### Daily use

```
$ gitea-mirror list pending          # what needs a decision
$ gitea-mirror review                # decide them one by one
$ gitea-mirror list skip             # what I said no to
$ gitea-mirror set brettviren/foo --mirror --apply
$ gitea-mirror set 'brettviren/old-*' --skip --prune --apply
$ gitea-mirror set brettviren/foo --rules     # forget my override
$ gitea-mirror set foo --adopt-existing       # bare name = repo under any owner
$ gitea-mirror show brettviren/foo
$ gitea-mirror add someorg/thing --apply      # outside any source
```

Categories for `list`/`review`: `pending`, `mirror`, `skip`, `mirrored`,
`missing` (should be mirrored, isn't), `left` (skip but still on Gitea),
`manual`, `gone`, `failed`, `untracked`, `all`.

### Config

See `gitea-mirror init` for an annotated starter.  Paths: config at
`$XDG_CONFIG_HOME/gitea-mirror/config.toml`, state and history in
`$XDG_STATE_HOME/gitea-mirror/`.

# BANTU

BANTU is a small, token-frugal agent orchestrator. You give it a task; it plans (or follows your plan), spawns
sub-agents (coder, reviewer), runs your tests in a sandbox, retries a limited number of times, and leaves the
result on a git branch for you to review. It is plain Python (standard library only) and works with any
OpenAI-compatible API, so you can spread work across free tiers.

Why not leave an always-on agent running? Because idle loops that call a model burn tokens for nothing.
BANTU's daemon idles by watching a folder, which costs **zero tokens**. Models are called only when a task exists.

## What is in the box

```
bantu/            the orchestrator (llm router, tools, agent loop, orchestrator, daemon, cli)
bantu.config.json providers, model pools, budgets, sandbox
roles/            prompts for planner, coder, reviewer (edit these to change behaviour)
tasks/inbox/      drop task files here; the daemon runs them oldest first
tasks/backlog/    ready-made tasks: 000 dry run, 001-003 Sente, 010 Node demo
projects/sente/   spec, project notes and acceptance tests for the Sente prototype
projects/demo/    tiny Python project for the zero-token dry run
projects/demo-node/  the same idea in JavaScript: shows how another language plugs in
docker/           sandbox images (Python, Node, Go example); deploy/ systemd unit; .vscode/ one-click tasks
roles/            also coder-node and coder-go prompts
```

## Set up in VS Code (about 10 minutes)

1. Install Python 3.10+, Git and Docker Desktop (Docker is strongly recommended: agent-written code runs inside it).
2. Unzip this folder, then in VS Code: File > Open Folder. Open a terminal (Ctrl+`).
3. `git init && git add -A && git commit -m "start"` (BANTU puts each run on its own branch, so it needs a clean repo).
4. `docker build -t bantu-sandbox -f docker/Dockerfile.sandbox docker/`
5. Copy `.env.example` to `.env` and paste in the keys you have. Never commit `.env` (it is git-ignored).
6. `python -m bantu doctor` and fix any FAIL line.
7. Zero-token test: `python -m bantu run tasks/backlog/000-demo-dry-run.md --mock`.
   (No Docker yet? set `BANTU_SANDBOX=local` for the dry run only.)

VS Code shortcut: Terminal > Run Task shows "BANTU: doctor / dry run / start daemon / add task / status".

## Point it at free models

`python -m bantu models` lists the model ids each provider currently offers. Free-tier model names and limits
change often, so edit `pools` in `bantu.config.json` to match, and lower each provider's `daily_tokens` and `rpm`
to what your account really gets. BANTU tries pool entries in order and fails over on rate limits.
Use different providers for resilience; do not open extra accounts on the same provider to dodge limits.
Each pool is tried in order: BANTU uses Gemini first and falls back to DeepSeek if it is unavailable, rate
limited, or has reached its provider-specific cap. DeepSeek's OpenAI-compatible endpoint is configured with
`deepseek-flash`; use `python -m bantu models` to confirm which model IDs your account currently offers.
The configured RPM and daily-token values are local safety caps, not a promise of the provider's quota; adjust
them to match your account.

## Build the Sente prototype

The steps below use the bundled project when `BANTU_PROJECTS_ROOT` is unset. For a separate Sente repository,
follow [Run projects from separate repositories](#run-projects-from-separate-repositories) and queue tasks with
`project: sente` and the repository URL.

1. Run the first task now: `python -m bantu run tasks/backlog/001-sente-rules-and-service.md`
   (or move it into `tasks/inbox/` and start `python -m bantu daemon`).
2. Open the Source Control view. The work is on a branch named `bantu/<task-id>`. Read the diff and
   `projects/sente/REVIEW.md`, then merge it (`git merge bantu/<task-id>`) or discard it.
3. Merge before starting 002 and 003, because they build on 001. The acceptance tests are in
   `projects/sente/tests/` and agents cannot edit them. 003 has no oracle tests: review it yourself.

## Run projects from separate repositories

BANTU can use external product repositories without adding their files or task branches to the BANTU repository:

1. Set `BANTU_PROJECTS_ROOT=C:\work\bantu-projects` in BANTU's local `.env` (or environment). This is a parent
   directory where BANTU will keep separate product repositories.
2. Give a task a project slug and repository link, for example:
   `python -m bantu add "Review the codebase and propose safe improvements" --project sente --repository https://github.com/your-org/sente.git --push`
   Omit `--push` to keep the resulting task branch local. BANTU clones the URL on first use; later tasks reuse
   that clone and verify that its `origin` matches the requested repository.
3. Ensure Git credentials are configured on the machine using SSH keys or a credential manager. Do not embed
   tokens or passwords in repository URLs.
4. Put `bantu.project.json` in the product repo to define its test command, protected files, and role. External
   repositories cannot override the sandbox mode, image, network, timeout, or allowed commands in BANTU's host
   configuration. Use `python -m bantu doctor` to check that configured external projects are independent Git
   repositories.

When configured, this root replaces the bundled `projects/` directory for task lookup. The BANTU host process
performs clone, commit, and explicitly requested push operations using the host's Git credentials; model prompts
and test containers do not receive those credentials. Before branching, BANTU fetches `origin` and fast-forwards
the current base branch when possible; divergent branches are refused rather than rewritten. Existing product
clones must have a clean working tree before a run.

Every acceptance or agent-requested command runs against a disposable project copy that excludes `.git`, `.env*`
configuration files, virtual environments and common caches. The copy is discarded after each command, so tests
cannot modify the host checkout or Git metadata. Docker mode adds resource limits and no network by default. Local
mode still runs arbitrary allowed executables on the host and is not a security boundary; use Docker for untrusted
repositories.

BANTU creates a `bantu/<task-id>` branch, commits there, and returns to its starting branch. With `--push` or
`push: true` in the task header, it pushes only that branch after acceptance checks and the reviewer gate pass.
It never force-pushes or writes directly to the default branch. If a provider runs out, BANTU commits the current
branch locally and records resume state; rerunning the same task file resumes that branch, fast-forwards its base,
and rebases the task commits before continuing. Completed subtasks are checkpointed and skipped on resume. A failed
push leaves the local task branch available for retry.

To also open a pull request, use `python -m bantu add "Add a feature" --project sente --repository <url> --pr`.
This requires the GitHub CLI (`gh`) to be installed and authenticated. BANTU creates the PR only after acceptance
and review pass; it does not merge or deploy. Task files, run reports, usage counters, and BANTU's role prompts
remain in the BANTU repository. Only one BANTU daemon can own a task queue at a time.

## Connect BANTU to CRM

The CRM BANTU worker polls its authenticated internal API for human-approved tasks. Set these values in BANTU's
local `.env`:

```text
BANTU_CRM_API_URL=https://crm.example.com
BANTU_WORKER_TOKEN=<same secret configured in the CRM API>
BANTU_PROJECTS_ROOT=C:\work\bantu-projects
```

Start the worker with `python -m bantu worker`. Keep Docker sandbox mode enabled. The worker accepts HTTPS Git
repositories with public DNS addresses, without credentials or custom ports. A CRM user must confirm repository
rights and an owner or admin must approve each task. BANTU tests and reviews each change, then pushes a task branch.
It does not merge or deploy. Never expose the worker token in a browser or commit it to a repository.
Run the worker behind an outbound firewall that blocks private and reserved network ranges. DNS checks alone do not
prevent DNS rebinding or redirects to private addresses.

## Give BANTU new work

- Quick task: `python -m bantu add "Add input validation to the API" --project sente --budget 150000`
  (no plan given, so the planner model splits it up; this costs a few extra tokens).
- Cheaper and more reliable: write the subtasks yourself in a ```subtasks block, as the files in `tasks/backlog/` do.
- New project: create `projects/<name>/` with `bantu.project.json` (`test_cmd`, `protected` paths), a `SPEC.md`
  and tests. Tests first is what keeps token use low: they tell the agent exactly when it is done.

## Add another language

Agents write plain text files, so they can write any language. What is Python-specific out of the box is the
**toolchain that checks the work**: the sandbox image, the command allow-list, the coder prompt and the default
test command. To add a language, change four things (about 15 minutes):

1. **Sandbox image** with the toolchain installed. The sandbox has no network, so packages must be baked in at
   build time. Examples: `docker/Dockerfile.node` and `docker/Dockerfile.go`.
   `docker build -t bantu-node -f docker/Dockerfile.node docker/`
2. **Project config** in `projects/<name>/bantu.project.json`:
   ```json
   {"test_cmd": "node --test",
    "protected": ["tests/", "SPEC.md", "bantu.project.json"],
    "default_role": "coder-node",
    "sandbox": {"image": "bantu-node", "allow": ["node", "npm", "npx", "tsx", "tsc"]}}
   ```
   Anything not in `allow` is refused. The `sandbox` block overrides the global one for this project only.
3. **Role prompt** `roles/coder-<lang>.md`: copy `coder-node.md` and change the language rules.
4. **Spec and tests first**, in that language's own test framework, then a task whose `acceptance` is the test command.

| Language | test_cmd | allow | Note |
|---|---|---|---|
| Python | `python -m pytest -q` | python, pytest, ruff | default |
| JavaScript | `node --test` | node, npm, npx, tsx, tsc | built-in runner, no packages needed |
| Go | `go test ./...` | go, gofmt | standard library only offline |
| Rust | `cargo test --offline` | cargo | vendor dependencies into the image |
| Java, .NET, PHP | `mvn -o test`, `dotnet test`, `phpunit` | matching tools | big images and slow: use sparingly |

Prove it for zero tokens first: `BANTU_SANDBOX=local python -m bantu run tasks/backlog/010-demo-node.md --mock`
(needs Node installed on your machine). Packages are the usual snag: a React or Next.js app needs `node_modules`
baked into the image, and free models burn many tokens on framework builds. For demos, plain HTML/JS is far cheaper.

## How token use is kept down

- No heartbeat; idle = 0 tokens. One fresh, short session per subtask, never one endless chat.
- Per-subtask, per-task and per-day token caps; three fix attempts maximum, then it stops and reports.
- Sub-agents see only the files you list. Old steps are trimmed from context and tool output is truncated.
- Several actions per reply (write, write, run) means fewer round trips.
- Strong, long-context model for planning and review; fast model for bulk coding.
- `python -m bantu status` shows usage per provider; every run has `runs/<id>/report.md` and `calls.jsonl`.

If the daily free allowance runs out, the task is re-queued and the daemon sleeps until the caps reset.
Re-queued tasks restart from the beginning, so keep tasks small.

## Move BANTU to the cloud (24/7)

Any small Linux VPS works; BANTU itself uses almost no CPU. The limit is your tokens.

1. On Ubuntu: `sudo apt install -y python3 git docker.io`, then `sudo useradd -m -G docker bantu`.
2. Put this repo in `/opt/bantu` (git clone from a private repo), owned by `bantu`. Create `.env` there (chmod 600).
3. `docker build -t bantu-sandbox -f docker/Dockerfile.sandbox docker/`; run `python3 -m bantu doctor` as the `bantu` user.
4. `sudo cp deploy/bantu.service /etc/systemd/system/ && sudo systemctl enable --now bantu`
   Logs: `journalctl -u bantu -f`.
5. Send tasks by `ssh` + `python3 -m bantu add ...`, by pushing task files to the repo, or later by a small
   webhook or chat bot that writes files into `tasks/inbox/`. Pull results with `git fetch` and review branches
   on your own machine.
6. Set a daily cap you are comfortable with in `bantu.config.json` before leaving it unattended.

## Safety rules (keep them on)

- Agents can only touch files inside their project folder, never `.git` or `.env`, and cannot edit protected files.
- Task project names are restricted to direct children of `projects/`; invalid or escaping paths are rejected.
- Commands are limited to `python`, `pytest`, `ruff`, run in a container with no network, 512 MB and 1 CPU.
  `BANTU_SANDBOX=local` removes that container and runs commands on the host; it is not a security boundary
  and should be limited to disposable dry runs.
- Models never receive provider keys or Git credentials. BANTU only pushes a product branch when the task
  explicitly requests it; it never force-pushes or merges to the default branch. You review and merge every branch.
- With review enabled, a task is marked successful only when the reviewer writes `REVIEW.md` with
  `Verdict: pass`. A missing, unfinished, or `needs work` review fails the task; inspect the report and branch
  before accepting changes.
- Usage state is written atomically and corrupt or missing-after-start state stops the run instead of resetting
  token counters. Run one BANTU daemon at a time to avoid cross-process usage-counter races.
- Use test data only. Free tiers often let providers use prompts for training, so never put real customer data,
  secrets or confidential partner figures into tasks.
- A free model can write plausible but wrong code. For anything that moves money, the tests and your own review
  are the safety net, not the model.

## Troubleshooting

- `OutOfBudget` or "no provider available": no key set, caps reached, or all providers cooling down after 429s.
- "model did not produce valid action blocks": weak model; move a stronger one up in the pool.
- Acceptance keeps failing: read `runs/<id>/report.md`; if a test looks wrong, fix the spec and tests yourself.
- Docker on Windows: run VS Code from a normal path (not a network drive) and keep Docker Desktop running.

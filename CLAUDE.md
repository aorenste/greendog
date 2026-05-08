greendog is a tool for making it easier to investigate and fix master CI
failures on pytorch/pytorch.  Here is the design space we live in:

- The first iteration of the tool does NOT assume we have a working build of
  PyTorch that we can iterate on.  So we are basically looking for
  interventions that we can *one shot* without having the ability to locally
  test our changes.  This limits the set of potential interventions we can do,
  but that's good because we also want this tool to operate autonomously, and
  if we do complicated interventions it's more important for a human operator
  to intervene.

- We care about "situational awareness" about trunk.  E.g., consider all
  commits in the last 24 hours, what is not working (even if we can't easily fix it?)
  For example, pytorch/pytorch has a concept of ci: sev which is used to communicate
  breakage, we want our agents to have access to this info (example:
  https://github.com/pytorch/pytorch/issues/182227)  For example, the HUD view
  is intended to be a way for humans to visually understand trunk redness, but
  it has gone beyond human parseability.  Another important part of
  situational awareness is the periodic jobs, which we have far less signal
  on, it's much more important to sift out as much info as we can get from the
  logs.

- When reporting "current trunk state", don't focus on HEAD — it typically
  has 1000+ pending/missing jobs and tells us nothing useful.  Instead, look
  back ~6 hours to find commits whose CI has substantially completed.  The
  "trunk HEAD" section of the report should really be "most recent commit
  with meaningful CI results" (i.e., the majority of jobs have a conclusion).

- To add on, flakiness at scale is important, because if something keeps
  flaking at a nontrivial percentage, we should work on it.  We can think of
  stack ranking flakiness in terms of incidence in some period, and using that
  to prioritize work we want to do.

- Our agents do NOT have internet access, for security reasons.  The harness
  is responsible for feeding in information.

- The HUD at https://hud.pytorch.org/ has lots of useful information, in a
  sparsely documented API we have access to that is maintained by Dev Infra.  We should
  document and make use of it as appropriate.  For example, on green-red edges, it seems
  that we already have AI assessments about whether or not something broke master or not.
  These show up like https://github.com/pytorch/pytorch/actions/runs/25282086754 (advisor run).  But it seems these advisor runs don't always run.

- We can only easily test this live.  We'll work on features as we discover
  particular trunk breakages.

- There is an autorevert system.  I don't know how good it is.  We'll be
  evaluating how good it is as we work on this.
  https://hud.pytorch.org/hud/pytorch/pytorch/main/autorevert

- There are some configs that have been presistently broken.  If something's
  been broken for more than a week, let's maintain state about these as
  persistently broken, and we will need a dedicated stab to try to fix them.

- There are a HUGE number of configs. It will be important to subdivide the
  problem appropriately into subagents.

## Repo workflow

- After making repository changes, automatically stage relevant files and
  commit them before reporting back, unless the user explicitly asks not to
  commit. Do not include generated artifacts, caches, virtualenvs, credentials,
  or unrelated user changes in the commit.

## Analysis methodology

When analyzing CI health, follow this approach:

1. **Pick a single representative commit.** Find one recent commit with
   near-complete CI (ideally 90%+ of per-commit jobs concluded). Analyze
   that commit's failures as the "current state of trunk." Don't aggregate
   failure counts across many commits — that conflates current redness with
   regressions that were already autoreverted.

2. **Filter to per-commit jobs by default.** The job grid includes periodic,
   nightly, and perf-benchmark workflows that don't run on every commit.
   Unless specifically asked about periodic jobs, exclude them. Per-commit
   workflow prefixes: `pull`, `trunk`, `Lint`. Exclude: anything with
   `periodic`, `nightly`, `perf`, `slow`, `benchmark` in the workflow name.
   **Caution**: some workflows look per-commit but are actually
   nightly/on-demand hybrids — e.g., `dynamo-unittest` and
   `inductor-unittest` are triggered by cron schedule + ciflow tags, NOT by
   every push to main. Check the workflow YAML triggers in
   `~/Dev/pytorch/.github/workflows/` before assuming a workflow is
   per-commit. A workflow is truly per-commit only if it has
   `push: branches: [main]` (or equivalent) as a trigger.

3. **Distinguish main shards from auxiliary runs.** Each test config runs
   three variants: the main test, a `mem_leak_check` rerun, and a
   `rerun_disabled_tests` rerun. When assessing trunk health, focus on
   main shard failures first. `mem_leak_check` and `rerun_disabled_tests`
   failures are secondary signals.

4. **Use the window to validate failures before reporting them.** Once you've
   identified failures on the representative commit, look across the window
   to check: does the same job fail on neighboring commits too? A job that
   fails on 1 commit but succeeds on the 5 commits before and after it is
   a **flake** — don't report it as breakage. Only report failures that are
   either persistent (failing across multiple commits) or that correspond
   to a clear green→red transition at a specific commit. One-off failures
   are noise at this scale.

5. **Cross-reference with HUD.** The HUD at hud.pytorch.org shows the
   same data visually. If analysis seems wrong (e.g., claiming a config
   is broadly red when HUD shows it green), the analysis methodology is
   likely flawed — revisit assumptions about which jobs and commits are
   being examined.

## Investigating autoreverts and landed-then-broken PRs

When a PR lands and gets autoreverted, the key question is always: why
did CI pass pre-merge but fail post-merge? Follow this checklist —
and after completing the investigation, write up learnings into this
file if the failure mode was novel.

1. **First verify: is the landed commit the same as the tested commit?**
   This is the most important check and should be done early. The PR head
   commit (what CI tests) and the merge commit on main (what actually
   lands) can diverge, especially for ghstack PRs. Compare them with:
   ```
   git diff <pr-head-sha> <merge-commit-sha> -- <relevant files>
   ```
   If they differ, the merge/squash/rebase onto main silently produced
   different code than what was tested. This happened with PR #182192:
   another PR (#181271) landed between the ghstack base sync and merge
   time, touching the same file. The squash onto main resolved conflicts
   silently but incorrectly — tests referenced a method that the
   conflicting PR had already removed.

   To find the conflicting commit: identify the ghstack base
   (`gh/<user>/<n>/base`) and main at land time (parent of merge commit),
   then `git log <base>..<main-at-land> -- <file>`.

2. **Pull actual CI logs to verify test execution.** Don't assume tests
   ran or didn't run — check. Use `gh run view --repo pytorch/pytorch
   --job <job-id> --log` and grep for specific test names. Verify:
   - Did the test file appear in the shard's test list?
   - Did the specific test methods get collected and executed?
   - What was the pass/fail result for each sub-shard?
   Note: `test_aotdispatch.py` is split into 8 sub-shards per CI shard.
   Log access requires no special auth for public repos via `gh` CLI
   (the REST API returns 403 for non-admins, but `gh run view --log`
   works).

3. **Understand the pull vs trunk workflow differences.**
   - `pull` workflow: triggered by `pull_request` event, `PR_NUMBER` is
     set, target determination (TD) is enabled (runs top 25% of tests by
     score). Uses `linux.arm64.m8g.4xlarge` runners for aarch64.
   - `trunk` workflow: triggered by `push` to `main` or `ciflow/trunk/*`,
     `PR_NUMBER` is unset, TD is disabled (runs 100% of tests). Uses
     `lf.linux.arm64.m7g.4xlarge` runners for aarch64.
   - Both run on the same commit SHA for the same PR (via ciflow), but
     the code checked out may differ for `pull_request` events (GitHub
     creates a temporary merge commit).

4. **Don't trust WebFetch summaries of PR content.** AI-summarized PR
   diffs and comments can be wrong about specific details (class names,
   method names, which tests failed). Always verify claims against actual
   code (`git show`, `curl` raw files) and actual logs.

5. **Check for masking by known-flaky tests.** A CI job can fail for
   multiple reasons. If a known-flaky test (e.g., `DivTensorV2`) fails
   in the same shard as a new regression, CI triage may attribute the
   job failure to the known-flaky test, hiding the real issue. The
   `merge -i` (ignore failures) flag then reasonably bypasses what looks
   like pre-existing flakiness.

6. **Check for merge skew (test passed on PR but fails on trunk).**
   This is the subtlest failure mode. The test ran on the PR, passed,
   but fails on the merge commit because other PRs landed between CI
   and merge. Investigation steps:

   a. **Confirm the test actually ran on PR CI.** Pull the logs for
      the specific shard and grep for the test name. Don't assume —
      tests are sharded across multiple jobs and TD may have excluded
      them. Check ALL shards of the relevant config (e.g.,
      `dynamo_wrapped` has 3 shards; `test_custom_ops` may be in
      shard 2, not shard 1).

   b. **If the test ran and passed, compute the skew window.** Get
      the BASE_SHA from the PR CI logs (grep for `BASE_SHA=` in the
      job log) and the parent of the merge commit on trunk:
      ```
      gh api repos/pytorch/pytorch/commits/<merge-sha> --jq '.parents[].sha'
      ```
      Then list commits in the window:
      ```
      gh api repos/pytorch/pytorch/compare/<base-sha>...<trunk-parent> \
        --jq '.commits[] | "\(.sha[0:10]) \(.commit.message | split("\n")[0])"'
      ```

   c. **Search for the culprit in the skew window.** Check which
      commits touch files related to the failure. For dynamo expected
      failure issues, check who originally created the marker file
      (`gh api "repos/pytorch/pytorch/commits?path=<marker-path>"`)
      and look for related PRs in the skew window that touch the
      same subsystem.

   d. **For ghstack PRs, check the entire stack.** If the PR is part
      of a ghstack, the top-of-stack CI includes lower commits. Pull
      CI logs from the TOP of the stack too — if the combined stack
      also passed, the failure is definitely from trunk skew, not
      from the stack itself.

   Example: PR #182293 was autoreverted for "unexpected success" in
   `test_impl_device_cpu`. The test ran and passed on PR CI (the
   expected failure was still failing as expected). But PR #181328
   (dynamo hash reimplementation) landed in the skew window, fixed
   the underlying dynamo tracing issue, and caused the test to start
   passing on trunk — making the expected-failure marker stale.

## Target determination (TD) reference

TD decides which tests to run in pre-merge CI. Key facts:

- Enabled when `PR_NUMBER` is set, not on main branch, not macOS/XPU/ONNX.
- Runs top 25% of tests by aggregated heuristic score; bottom 75% skipped.
- `EditedByPR` heuristic gives score 1.0 (maximum) to any test file
  directly modified by the PR (whole-file granularity).
- Scores are additive across heuristics. Score 0 = no heuristic cares.
- Code: `tools/testing/target_determination/` and consumed in
  `test/run_test.py` at the `get_top_per_tests(percent_to_run)` call.
- TD operates at test-file level primarily; `TestRun` can include/exclude
  specific test classes but most heuristics use full-file `TestRun`s.
- TD runs AFTER other filters. The `--dynamo` flag, `--exclude-*` flags,
  and shard assignment all happen before TD. TD only selects among the
  tests that survive those earlier filters. So if a test doesn't appear
  in TD's "tests to run" OR "excluded" lists, it was filtered out at an
  earlier stage (e.g., not assigned to this shard).
- When TD has no historical timing data for a job name (e.g., new OSDC
  runner infra), it falls back to running ALL tests. Check for the log
  line `Running all tests` vs `Running 25% of tests based on TD`.

## Investigation anti-patterns

Traps to avoid when investigating CI failures:

- **Don't theorize without logs.** Every theory about "TD skipped it" or
  "the test wasn't in this shard" must be verified by pulling actual job
  logs. Theories are cheap; logs are ground truth.
- **Check ALL shards, not just shard 1.** Tests are distributed across
  shards. `test_custom_ops` might be in shard 2 of `dynamo_wrapped`,
  not shard 1. The shard assignment is visible in the TD output or the
  `td_exclusions` artifact.
- **Don't confuse the autorevert confirmation run with the original
  failure.** The autorevert system re-dispatches a filtered workflow
  with `tests-to-include: <failing_test>` to confirm the failure isn't
  a flake. This confirmation run has different inputs than the original
  trunk CI. When investigating, find the ORIGINAL push-triggered trunk
  run, not the autorevert confirmation.
- **Investigate before concluding.** Early in an investigation, resist
  the urge to declare a root cause. Multiple plausible theories can be
  wrong (TD excluded it? no, `--dynamo` excluded it? no, wrong shard?
  no, it ran and passed — it's actually merge skew). Follow the evidence
  step by step.

## Marking CI jobs as unstable

When a job is persistently broken and not worth blocking on, there are
two mechanisms to mark it "unstable":

1. **Add "unstable" to the job name in the workflow YAML.** The trymerge
   bot (`trymerge.py:~1858`) checks `if "unstable" in name` and ignores
   failures for such jobs. Example: a test-matrix entry like
   `{ config: "foo", runner: "...", unstable }` produces a job name
   containing "unstable". This is the lightweight option for individual
   jobs within an otherwise stable workflow.

2. **Move the job from `trunk.yml` / its own workflow into
   `unstable.yml`.** The unstable workflow
   (`.github/workflows/unstable.yml`) runs on every push to main but is
   NOT in `mandatory_checks_name` in `merge_rules.yaml`, so it never
   blocks merging. Jobs graduate back to trunk when red rate < 5% and
   TTS < 3h.

Merge rules (`merge_rules.yaml`) only mandate `pull`, `Lint`, `EasyCLA`
(and sometimes `trunk`, `inductor`). Workflows like `dynamo-unittest`
are already non-mandatory — failures there don't block merging but do
create noise in trunk health and may trigger autorevert.

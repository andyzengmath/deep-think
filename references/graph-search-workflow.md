# Persistent graph-guided mathematical research

Use this workflow for sustained proof, disproof, construction, or theory
investigations. It complements `research-protocol.md`; it does not replace
analytic proofs with bookkeeping or implement an automatic scheduler.

## Canonical records

Keep one `research-graph.json` in the existing
`deep-think-transcripts\<project>` directory. Its `schema_version` is 1.
Use `research-graph.schema.json` for structural validation.

The graph is authoritative for **search state**, not mathematical truth.
Proofs, source citations, and exact certificates remain in their original
files and are referenced by evidence IDs. The runner alone owns
`state.json`, context files, transcripts, and its lock.

For a new project, adapt `research-graph.template.json`, changing
`record_type` from `template` to `research-project`, the slug, mission,
solution goals, and scopes. Never replace an existing graph with the
template. Never create a new project merely because a resumed session has
a different working directory.

An optional user index at
`<home>\deep-think-transcripts\research-projects.json` can locate existing
graphs. It contains only project slugs, titles, graph paths, and aliases;
statuses live only in the corresponding graph. Resolve index paths relative
to the index directory. Do not select an unrelated project by recency alone.

## Start or resume a session

1. Identify the exact project and existing record directory. Consult the
   original workspace or the user index before initializing anything.
2. Read this workflow, the research protocol, and the graph. A truncated
   file preview is not the complete graph; read remaining sections or query
   the relevant records structurally.
3. Validate the schema, unique IDs, reference targets, and current revision.
   A malformed or inconsistent graph blocks new work; do not silently reset it.
4. Read the mission, checkpoint, active jobs, frontier actions, and evidence
   needed by the selected branch. Reconcile with newer source records when
   necessary; historical "running" text does not establish process liveness.
5. Select a bounded action and record its contract and resource budget before
   a paid request or expensive calculation. A transcript's suggestion to
   compute the next coefficient is a proposal, not an automatic instruction.

Use the original runner workspace and stable project slug. The graph's
`project.workspace_root` is relative to the graph directory unless absolute.
Evidence paths are relative to `project.evidence_root`, itself resolved from
the graph directory, unless an evidence path is absolute.

## Proof-strategy loop

```text
read/reconcile graph
  -> expand precise alternative actions
  -> select by root relevance and information per remaining cost
  -> bounded construction, counterexample search, or calculation
  -> adversarial audit and any required repair
  -> update scoped claims, artifacts, evidence, and frontier
  -> reassess globally before another episode
```

A node is an exact claim, goal, or artifact obligation, not a chapter.
Record hypotheses, fixed and free choices, and quantity conventions.
Different scopes need different IDs. A fixed-jet obstruction and an
escape using another jet can both be valid.

An inference's premises form an AND. Multiple inferences to the same
conclusion are OR alternatives. `mission.solution_node_ids` are alternative
complete verdicts, not a request merely to prove an excluded-middle
disjunction. Shared lemmas can be reused through a justified specialization.

Check the inference, not just its premises. Witness bindings must refer to
the same metric or object. Extra assumptions must be discharged or remain
in the conclusion. Failed attempts remove only their proposed routes;
rejecting all currently known alternatives is not an exhaustive impossibility
proof. Accepted proof dependencies must not be circular.

Rank eligible actions with the provisional heuristic
`(4R + 3D + 2U + G) / (1 + C)`: root relevance, discriminatory value,
unblocking, generality, and remaining cost including audit. Ratings R/D/U/G
are 0--3; start with coarse cost units 1, 2, 4, or 8. Record uncertainty.
This is not an admissible A* heuristic or a calibrated success probability.

Keep at most three actively developed strategies initially. Suggested
resource allocation is 50% developed route, 30% root bridge or independent
strategy, and 20% audit/consolidation. Required audit is not capped by that
reserve; reduce new exploration instead of skipping it.

An initial episode allows approximately three substantive reasoning turns,
with a separate resource budget. A substantive repair needs a new audit;
carry it forward if it does not fit. A null budget means **not set**, not
unlimited authorization. Graph settings do not grant spending permission.

After three episodes without meaningful progress, suspend and reconsider the
branch. After two consecutive repairs merely shift the same finite-order
zero set, require an abstraction checkpoint: a mechanism, uniform estimate,
new freedom, or decisive obstruction. Another coefficient must earn its cost.
Parking a branch is not mathematical refutation.

## JSON contract

| Field | Meaning |
|---|---|
| `schema_version`, `revision`, `updated_at` | Format version, monotonically increasing graph revision, UTC update time |
| `record_type`, `project` | Template versus live research record; stable slug and path anchors |
| `mission` | Exact root question, open/candidate/resolved state, alternative solution-goal IDs |
| `policy` | Selection policy and bounded-episode defaults, not runtime enforcement |
| `nodes` | Typed goals/claims/artifacts with scopes and separate applicable status vocabularies |
| `inferences` | AND premise sets, conclusions, witness/scope bindings, argument state |
| `actions` | Bounded questions, prerequisites, output contracts, scores, budgets, operational states |
| `evidence` | Stable source IDs, paths, provenance, and optional whole-file SHA-256 hashes |
| `events` | Append-only decision/outcome history with revision and time |
| `checkpoint` | Current pause/running state, frontier, unresolved next steps, known jobs |

Claim/goal statuses are `open`, `candidate`, `conditional`, `accepted`,
`disputed`, or `refuted`. Artifact statuses are `missing`, `ready`, or
`stale`. An accepted analytic existence theorem can coexist with a missing
expanded artifact; do not downgrade the theorem merely because a computation
has not been materialized.

An imported theorem may be recorded as an accepted leaf at its stated scope,
with `acceptance_basis` explicitly saying that its audit is inherited.
Do not imply that the bootstrap reconstructed its entire proof dependency
closure or reran its computations.

Before accepting a claim, require a checkable argument at its exact scope,
supporting evidence, and the independent scrutiny required by the research
protocol. Numerical searches propose data; exact checks support proofs.
Hashes establish byte identity, not truth. Record recovered calculations
as recovered, and inherited arguments as inherited.

Before updating `mission.status` to `resolved`, its `resolution_node_id`
must be a member of `solution_node_ids`, name an accepted goal, and have a
complete root-scope argument. Inspect its full proof obligations and trust
basis. Task counts, finite-order compatibility, or a successful native run
are not a resolution certificate.

Use `mission.status: candidate` only for a genuine candidate solution of the
root question awaiting scrutiny, not for another accepted local lemma.
Keep `resolution_node_id` null until resolution.

Schema validation cannot prove those mathematical facts or dynamically
validate all cross-references. Also check:

1. IDs are unique within each record collection.
2. Node assumptions, inference premises/conclusions, action targets/
   prerequisites, evidence references, and frontier action IDs exist.
   Known job action IDs must also exist; only an unreconciled legacy job may
   have a null action ID with an explanatory note.
3. Accepted conclusions do not rest on unproved or circular obligations.
4. Inferences preserve scope and shared witnesses.
5. An action marked completed has the promised output or an explicit
   inconclusive/refuted outcome; a running job has honest tracking data.

For PowerShell installations, structural validation uses the existing
`Test-Json` command:

```powershell
$graph = Get-Content -LiteralPath "deep-think-transcripts\project-slug\research-graph.json" -Raw
if (-not (Test-Json -Json $graph -SchemaFile ".github\skills\deep-think\references\research-graph.schema.json")) {
    throw "Invalid research graph"
}
```

Adjust only the graph and installed-skill paths to the actual workspace.
Use an existing JSON Schema validator on other platforms.

## Persist after every episode and before every handoff

Only one coordinator writes a graph. Record the loaded revision. Preserve
the previous valid revision before replacement; stage and validate the
updated JSON, including cross-references. Recheck that another writer has
not changed the source revision. Stop and reconcile conflicts instead of
overwriting them. No automatic lock or update service is supplied here.

Increment `revision`, update `updated_at`, append an event, and update
affected nodes, evidence, action outcomes, budget usage, and the checkpoint.
Record an inconclusive or failed episode too. Do not put the only progress
record in the final chat message.

Use `kind: episode` for a completed research episode. Its event records the
actual outcome in `summary`, affected action/node IDs, produced or reused
`evidence_refs`, and `usage` (reasoning turns, native runs, and wall minutes).
Use null for an unknown measurement, not a fabricated zero. Add newly
produced proof/calculation artifacts to `evidence`; do not limit updates to
old or disputed evidence. Preserve older byte versions when replacing a
source, and assign revised evidence a new ID rather than silently rebinding
an old certificate.

A root-inconclusive episode may still establish a scoped lemma or produce a
useful artifact: update those nodes honestly while keeping the root open.
Append events to the existing history; never replace the history with a
one-event update example.

For a scope change, fork a new node and explain the relationship. Preserve
valid older theorems. If an actual proof or implementation defect is found,
mark affected evidence disputed/stale and block dependent promotions;
correcting a statement does not make its old false version true.

Pin cached results to their input versions, quantity conventions, parameters,
and relevant source closure. Reuse only at a justified scope. Native jobs
with incompatible frozen source snapshots require separate processes.

Before starting a job, record its action and input version. Record remote
response IDs when available. If a polling process is interrupted, do not
equate that with remote cancellation or blindly resubmit. Keep the job
`unknown` until its state is established. Existing runner recovery remains
authoritative; this graph does not add unsupported recovery commands.
If an older job's input graph revision is unknown, record null and explain
the uncertainty rather than guessing a revision.
If a legacy job is known to belong to this project but its action mapping is
also unknown, retain it with `action_id: null` and an explanatory note.
Do not drop it from the checkpoint or fabricate a dummy action. Block new
submissions to the affected backend until it is reconciled. New jobs must
have a registered action and input revision before launch.

On a normal handoff, record completed work, unresolved obligations,
frontier actions, selection rationale, and any in-flight jobs. Unexpected
termination may leave a stale checkpoint; reconcile it before new work.
Never edit protected runner state/context files to repair graph metadata.

## Stop labels

Use `paused`, `waiting`, or `blocked` for operational limits and missing
inputs. Close a mathematical branch only under a proved exclusion at its
precise scope. Resolve the mission only with its actual solution certificate.
No finite budget or empty known frontier proves that all ideas are exhausted.
One blocked action does not make the whole project blocked: retain other
eligible frontier actions and reassess them before declaring a project-wide
block.

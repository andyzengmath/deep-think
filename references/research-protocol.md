# Mathematical research protocol

## Frame the first turn

Include:

- The exact target: proof, disproof, construction, classification, bound, or
  research map.
- Definitions, notation, base field, regularity assumptions, conventions, and
  allowed foundational results.
- Known lemmas and source excerpts, with provenance.
- Constraints on methods and examples that must be covered.
- A success criterion that distinguishes a complete result from partial
  progress.

Ask for explicit labels:

- **Proved:** A complete argument is present in the transcript.
- **Externally established:** A precise source is identified but not reproved.
- **Computationally checked:** A finite calculation or experiment supports it.
- **Heuristic:** Evidence exists without a proof.
- **Conjectural:** A proposed statement remains unproved.
- **Blocked:** A named gap prevents continuation.

Do not request hidden chain-of-thought. Request a checkable derivation, proof,
calculation, counterexample, or concise reasoning summary.

## Iterate by role

Use separate turns when the investigation is difficult:

1. **Landscape:** Normalize the problem, inventory relevant theory, and identify
   equivalent formulations or obstructions.
2. **Construction:** Develop one candidate in detail, including all
   well-definedness and compatibility checks.
3. **Adversarial audit:** Search for minimal counterexamples, circular
   dependencies, hidden compactness or choice assumptions, type mismatches, and
   unjustified limit exchanges.
4. **Repair:** Patch a specific gap without silently weakening the target.
5. **Synthesis:** Produce the strongest defensible result, its dependency graph,
   and the next decisive subproblem.

Prefer a focused follow-up over asking one turn to explore every possible route.

## Audit completion claims

Before accepting a claimed theorem or open-problem solution, require:

1. A line-by-line dependency list.
2. Verification of every boundary, degenerate, singular, and low-dimensional
   case.
3. A search for counterexamples under the exact hypotheses.
4. Confirmation that cited results have the stated hypotheses and conclusions.
5. A self-contained final proof separated from exploratory discussion.
6. Independent expert or formal verification when the claim is novel or
   consequential.

Record a partial theorem or obstruction honestly when a complete solution is
not established.

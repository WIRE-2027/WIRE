# 3. CV: Candidate ranking review

## Role

You are an RTL fault-localization engineer.

## Task

Re-examine candidate responsibility using the original RTL, specification and complete requirements. Verify the independent source diagnoses, combine the full counterfactual process with the requirements and M1/M2 materials, then refine the supplied heuristic initial ranking and return the final complete ranking.

## Input format

```jsonc
{
  "spec": "", // Complete specification
  "rtl": {}, // Original file path -> complete source with original line numbers
  "build": {}, // Original build configuration
  "interface_constants": {}, // Optional original package constants and source locations
  "interface_declarations": {}, // Optional original enum, type and parameter declarations
  "requirements": [], // id, requirement, trigger, expected, reason
  "entries": [], // Original observations and associated requirements
  "tests": [], // Original TBs, per-test build configurations and assigned requirements
  "candidates": [], // Every original id, file, lines, entries, requirements and reasons; original_text contains exact numbered original spans
  "m2_context": {}, // Traces, local AST, augmentations and analyses
  "saved_runs": [], // Saved checks and execution records
  "baseline": [], // Original RTL replay observations and logs
  "counterfactual": [], // Independent edits, executions and common-observation comparisons
  "observation_sets": [], // One shared set of signal/time points and expectations per trace
  "observation_limitations": [], // Unavailable expectations or trace context
  "initial_ranking": [], // Descending heuristic score, counts, upstream_FP and observation completeness
  "identical_variant_groups": [], // Candidate IDs with exactly identical patched sources
  "shared": {}, // Optional shared run records; $ref expands to the exact value under shared
  "previous_ranking": [], // Optional previous complete ranking for substantive re-examination
  "findings": [], // Independent source diagnoses, with original lines, exact quotations and legal scenarios
  "diagnosis_limitations": [] // Missing conditions reported by the independent diagnosis
}
```

Candidate lines are inclusive original source ranges. Interpret recorded times using the TB time units and formatting. Probe times use native VCD ticks. CHECK table columns correspond to rows, and absent_columns_by_row identifies fields absent from original records. Execution status, check values, diagnostic logs and probe samples are included in the input.

Repeated check rows, diagnostics and logs may use {"$ref":"#/shared/S_x"}. Its complete value is provided once under the corresponding key in shared within this input.

CHECK expected_valid means its expected value has a recognized numeric or full-width four-state encoding. valid records structural and uniqueness validation and remains true only if subsequent waveform validation succeeds when performed. waveform_valid records whether the signal exists, the tick is in range and the recorded actual value matches the VCD value at that tick. These fields describe record and waveform checks. The allowed stimulus, required behavior and semantic correctness of expected values still require original-interface and specification review. Missing validity fields leave that validation unavailable.

## Current input

{{input}}

## Rules

1. Read the complete specification and original RTL to establish each operation's behavioral obligation. Respect the module scope of each Target file section. Determine the actual configuration, reset history, accepted transaction, control conditions and relevant cycle. Use the requirements to locate the responsible implementation and inspect its producers, enclosing guards, state updates and connections. Resolve widths, signedness, enum values and parameter settings from the supplied declarations.
2. For each plausible leader, establish the comparison in its reason using the original interface and source: identify the allowed input history or accepted transaction and the original declaration, producer, enclosing guard or specification clause that permits it; derive the required behavior under that same condition; then derive the divergent result from the candidate's original operation. Resolve every premise introduced by a TB, requirement explanation or proposed edit against those original sources, including input encoding, parameter configuration, producer guarantees, grant/ready relationships and effective clock edges. When an essential premise remains unavailable, state it explicitly in limitations and retain an unresolved assessment for that explanation. Use counterfactual observations to locate the changed branch and its propagation after this comparison holds. Compare leaders by the completeness and directness of that established contradiction, accounting for uncertainty in both competing explanations.
3. Read every observation with its actual provenance. actual, actual_bits and recorded history describe measured values; expected and expected_bits describe expectations to verify under the applicable conditions. Check each CHECK's validity, stimulus and sampling. Read sequential inputs before the effective edge and updated state after it. For scoring, compare the same signal at the same VCD tick within each trace using the common expected value. Never substitute a shifted transaction or a different tick. CHECK-ID comparisons and timing_changes are supplementary diagnostics, not the scoring unit. Derive source-level contradictions from the exact original operation and its legal conditions.
4. Explain each intervention through its actual edits and execution history. Identify the original operation changed by every edit, the affected producer or state and the propagation to the observation. Associate the comparison with that operation's candidate anchor. Attribute multi-location edits separately. Combine repeated CHECKs for a common mechanism and examine the subsequent state and transaction behavior alongside the immediate output.
5. Start from initial_ranking, computed as FP + upstream_FP - PF: an upstream FP contributes two points, any other FP one, and PF minus one; FF and PP contribute zero. Each signal/time point counts once per trace. Unavailable observations are excluded and reported, not counted as passes; a null score has no comparable observations. Inspect completeness and limitations before interpreting a partial score. Refine this initial order using the specification, original operations and intervention effects. Explain any departure from the initial order for leading candidates. Compare the strongest explanations before deciding their order. Prefer the clearest applicable obligation, most specific original semantic contradiction, direct causal responsibility and fewest unresolved premises. Check constants, boolean polarity, enables, arithmetic, bit selections, next-state transitions and port sources in the original code. Verify the exact difference separating close candidates before treating their comparison as a tie. Then order the strongest remaining independently supported operations, with the most direct representative of a shared operation ahead of its downstream or redundant members. Verify each finding's exact original operation, applicable specification quotation, legal condition and source-derived result. Challenge its strongest defense again. Associate a finding with an existing candidate by the same original operation and its guard or connection responsibility. Read the complete enclosing conditions and actual assignment around the candidate anchor, and distinguish separate upstream and downstream operations. A finding's line numbers identify its source context; proximity alone cannot establish the association. A missing default or guard can be carried by the original candidate operation responsible for that behavior. Retain every candidate's original coordinates. Treat materially unresolved premises as unresolved. Compare verified direct source contradictions with the other candidates using the requirements and intervention process. When previous_ranking is present, re-examine its order using the same original-source comparison and retain its relative order only where the substantive comparison remains unresolved. A previous position contributes no correctness support. Without previous_ranking, preserve the heuristic initial order where the substantive comparison remains unresolved.
6. Return every original candidate ID exactly once with its original file and ranges. Use root_cause for an operation that violates its own responsibility and produces the explained deviation, propagation for faithful transmission, compensation for downstream compensation and unresolved for missing essential premises. Use no_conflict when a concrete source condition or specification clause overturns the original suspicion and place it last. For leading candidates, reason states the applicable condition, required result, original-source result, decisive intervention observations and the strongest competing explanation. Keep remaining reasons concise and specific.

Use the supplied original function bodies and their dependencies to resolve the actual operation semantics. changed_locations describes actual changed original spans with old/new text and overlapping candidate_ids; insertion_after_line marks an insertion boundary in the original file. identical_variant_groups lists candidates whose complete patched sources are identical. Attribute combined or identical-variant responses to the particular changed original operations. Recovery under a combined edit supports that combination; establish the contribution of an individual anchor from its operation and observations. A necessary supporting edit outside an anchor can remain part of the same mechanism.

## Output

Return only JSON. Comments describe fields.

```jsonc
{
  "ranked_candidates": [
    {
      "candidate_id": "", // Original candidate ID, array order is the final ranking
      "role": "", // root_cause, propagation, compensation, unresolved or no_conflict
      "requirements": [], // Associated requirement IDs
      "reason": "" // Allowed original-interface condition and its source, required behavior, original-operation result, intervention propagation and final comparison
    }
  ],
  "limitations": [] // Specific unresolved conditions or observations
}
```

## Output example

```json
{
  "ranked_candidates": [
    {
      "candidate_id": "C1",
      "role": "root_cause",
      "requirements": ["R1"],
      "reason": "R1 requires q to hold while disabled, but the original write loads d with en=0. The added guard restores retention and the next legal load, with out following q, so the update condition ranks first."
    },
    {
      "candidate_id": "C2",
      "role": "propagation",
      "requirements": ["R1"],
      "reason": "The original out operation correctly copies q, transmitting the upstream update error. Editing out leaves subsequent q deviations present, so it follows the write that produces the deviation."
    }
  ],
  "limitations": []
}
```

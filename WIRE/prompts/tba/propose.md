# Prompt 3: Augment missing dependencies

## Role
You are an RTL verification engineer specializing in data and control dependency augmentation.

## Task
Propose augmentation sites and concrete edits for missing dependencies. Review boundaries, unlocated_entries and pending requirement_hints together. Boundary starts use original locations and boundary_ast; pending requirements use original loc, triggers and expected behavior. Follow related data, control and connection paths through the complete RTL, including other supplied files and modules.

## Input format

```jsonc
{
  "spec": "", // Complete specification text
  "rtl": {}, // All supplied RTL files with original line numbers
  "requirements": [], // Complete requirements: id, verbatim passages, loc, trigger, expected and reason
  "requirement_hints": [], // Pending requirement IDs, original loc, conditions and check status
  "entries": [], // Mismatch entries: id, test, signal, time, expected, actual and requirements
  "candidates": [], // Existing candidates for this group
  "boundaries": [], // Missing-relation boundaries for this group; may be empty
  "tb_errors": [], // TB or CHECK issues for this group; may be empty
  "explained": [], // Explained entry IDs; may be empty
  "unresolved": [], // Unresolved entries; may be empty
  "boundary_ast": [], // AST context extracted from boundaries; use it as the starting local view
  "unlocated_entries": [], // Unlocalized entry IDs prioritized for augmentation
  "tests": [], // Executed testbenches, configurations and check records
  "guard_definitions": {} // Optional complete conditions referenced by guards.ref
}
```

**Actual input**
{{input}}

## Rules
1. Review every supplied boundary, using entry, file, line, missing_signal and reason to establish its starting point. Inspect boundary_ast and follow producers, consumers and control connections through the complete RTL. Associate a real entry and any related entries from the same test.
2. Also review unlocated_entries and requirement_hints. Follow an entry's boundary when available; start other mismatches from their observations and related requirements. Join pending hints to requirements by ID, then use original loc, check status, trigger and expected to identify a missing relation. Reference original IDs in requirements; optionally select an existing TB ID in test. Describe preparation, stimulus, observation time and Spec expectation in test_plan. Use requirements to identify a requirement-only start; entry may be omitted.
3. Establish a specific missing data, control or connection relation from the complete Spec and RTL. Missing valid CHECKs identify behavior to investigate; the implementation and required behavior determine whether augmentation is warranted. Each proposal adds one relation with needed declarations, ports and connections. anchor identifies its original consumer or guard. relation.source/target use exact instance paths; kind is data or control.
4. Each edit specifies original file/line, exact old text and complete new text. Each proposal starts independently from the original RTL; linked edits may cross supplied files and hierarchy levels. Retain compiler aggregate targets for arrays/structs and express elements in edits. Preserve other Spec behaviors and reset/event priorities.
5. For a new DUT input, specify the existing TB stimulus signal and new port in test_connections. Plan the same stimulus and checks for comparing original and augmented RTL; expectations follow the Spec and input history. Describe any additional checking conditions in test_plan.
6. Explain the required behavior, missing relation, choice of augmentation site and expected value or path change. Put questions needing further information in deferred; return an empty proposals array when no supported augmentation exists.

Resolve shared guards.ref through guard_definitions for complete conditions.

## Available tools
Use the supplied Spec, RTL, AST and check records. Return augmentation proposals directly.

## Output format
Return a JSON object. proposals may contain both boundary-start and pending-requirement proposals. Merge duplicate proposals for the same missing relation while retaining their related entry and requirement IDs. Fields are defined below; concrete instances appear in Output example.

```jsonc
{
  "proposals": [
    {
      "entry": "", // Boundary or unlocated mismatch entry ID; omit for a requirement-only start
      "entries": [], // Optional related entry IDs from the same test, including entry
      "requirements": [], // Original requirement IDs; required for requirement-only starts
      "test": "", // Optional existing TB ID suitable for supplemental checks
      "test_plan": "", // For requirement starts: preparation, stimulus, observation time and Spec expectation
      "anchor": {
        "file": "", // Original RTL file
        "line": 0 // Original augmentation anchor line, one-based
      },
      "relation": {
        "source": "", // Added producer or control signal, full instance path
        "target": "", // Consumer signal, full instance path
        "kind": "" // data or control
      },
      "edits": [{
        "file": "", // Original file to edit
        "line": 0, // Original line locating old text
        "old": "", // Exact source text to replace
        "new": "" // Complete replacement including the added relation
      }],
      "test_connections": [], // Optional new input connections: instance, port, signal
      "reason": "" // Spec requirement, missing relation and expected change
    }
  ],
  "deferred": [] // Open proposals: entries or requirements, plus reason
}
```

## Output example
Boundary start: the downstream y mismatch reaches producer.q, whose line-4 update lacks enable control.

```json
{
  "proposals": [
    {
      "entry": "E1",
      "entries": ["E1"],
      "requirements": ["R1"],
      "anchor": {
        "file": "producer.sv",
        "line": 4
      },
      "relation": {
        "source": "tb.dut.u_producer.en",
        "target": "tb.dut.u_producer.q",
        "kind": "control"
      },
      "edits": [
        {
          "file": "producer.sv",
          "line": 4,
          "old": "else q <= data;",
          "new": "else if (en) q <= data;"
        }
      ],
      "reason": "The Spec requires q to hold while disabled. Add the missing en control at the q update."
    }
  ],
  "deferred": []
}
```

Requirement start: R2's disabled-hold check has not executed; T1 can be extended, and line 4 writes q unconditionally.

```json
{
  "proposals": [
    {
      "requirements": ["R2"],
      "test": "T1",
      "test_plan": "After reset, enable loading q=3. Then drive en=0 and data=5; after the next rising edge check q=3.",
      "anchor": {
        "file": "counter.sv",
        "line": 4
      },
      "relation": {
        "source": "tb.dut.en",
        "target": "tb.dut.q",
        "kind": "control"
      },
      "edits": [
        {
          "file": "counter.sv",
          "line": 4,
          "old": "else q <= data;",
          "new": "else if (en) q <= data;"
        }
      ],
      "reason": "R2 requires holding while disabled, but the original branch writes data each cycle. Add enable control and observe its effect with the same check."
    }
  ],
  "deferred": []
}
```

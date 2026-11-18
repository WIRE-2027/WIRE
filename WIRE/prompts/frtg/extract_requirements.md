# Extract requirements for targeted verification

## Role
You are an RTL functional verification engineer.

## Task
Compare the complete Spec with the original RTL. First check specified behaviors against their implementation, then inspect RTL branches and state updates for overlooked Spec conditions. Return concrete discrepancies and implementation-grounded conditions whose correctness still needs a test.

## Input format

```jsonc
{
  "spec": "", // Complete specification text
  "rtl": {} // File name → RTL with original line numbers
}
```

**Actual input**
{{input}}

## Rules
1. **Organize requirements by independent expected behavior.** Retain applicable conditions, exceptions and cross-cycle relationships. “Increment when enabled; otherwise hold” may form two requirements; a mode condition in one passage and its hold behavior in another may form one requirement. Group equivalent value/bit cases, and keep cases with distinct boundary behavior or expectations separate.
2. **Check Spec and RTL in both directions.** Match specified behavior to implementation, then inspect assignments, branches and state updates for overlooked conditions. For each control operation, check its active and ignored cases, including whether an ignored control can suppress a simultaneous valid state update. Consider reset/hold, handshakes/stalls, competing events, widths/boundaries, parameters/modes, exceptions and cross-cycle or cross-module relationships where the design uses them. Use this checklist to guide analysis; output the resulting requirements.
3. **Extract two kinds of verification concerns: identified discrepancies and implementation-grounded questions that need testing.**
   - **Identified discrepancies:** Point out where RTL behavior differs from the Spec, such as adding two when the Spec requires adding one.
   - **Implementation-grounded questions:** Static reading has not established a violation, but specific logic, a boundary condition or a state sequence suggests a possible deviation that needs testing. For example, when a FIFO is full and a read and write occur together, does the full flag still block the write?
   For both kinds, explain the relevant implementation, occurrence condition and possible deviation. Check whether other applicable branches already satisfy the behavior, and retain discrepancies or questions that remain after that check. A concern may be retained while its exact source location is unresolved.
4. **Separate quoted observations from the checking objective.** Each `requirement` string copies one continuous Spec passage verbatim. List jointly relevant passages separately in the same array; a passage may support several independent requirements. In `expected`, combine the applicable conditions and correct behavior from the complete Spec; in `reason`, explain the implementation concern. Use RTL to understand interfaces, paths and triggering; use the Spec to determine correctness.
5. **Locate the related implementation and suggest a trigger.** `loc` uses original file names and inclusive one-based line ranges, such as `[4,4]`. Multiple ranges are allowed. Cite related logic for missing behavior; use `{}` when unresolved. `trigger` describes a revisable state and input sequence.
6. **Return the complete set of distinct concerns.** Use IDs R1, R2, ... and include every qualifying requirement. The array represents the set to verify, with equivalent descriptions consolidated. Return `[]` when no qualifying concern remains.

## Available tools
The program supplies the complete Spec and numbered RTL; do not call external tools.

## Output format
Return only the following JSON array. Keep the field names and nesting unchanged; comments describe the required content.

```jsonc
[
  {
    "id": "", // string; unique requirement ID such as R1
    "requirement": [], // nonempty array of verbatim continuous Spec passages
    "loc": {}, // object: RTL file -> inclusive line ranges [[start, end]]; use {} when unresolved
    "trigger": "", // string; state preparation and input sequence
    "expected": "", // string; Spec-derived behavior to check
    "reason": "" // string; implementation mapping and possible deviation
  }
]
```
Merge equivalent behaviors and return every distinct qualifying item.

## Output example

```json
[
  {
    "id": "R1",
    "requirement": ["Outside reset, the counter increments by one modulo 16 when en is high."],
    "loc": {"counter.sv": [[4, 4]]},
    "trigger": "Reset, then set rst_n=1 and en=1 before a rising edge.",
    "expected": "count becomes 1, then increments by one modulo 16.",
    "reason": "Line 4 adds 2; the Spec requires adding 1."
  }
]
```

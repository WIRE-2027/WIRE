# Compatible test grouping

## Role
You are an RTL testbench planning engineer.

## Task
Group requirements with compatible test conditions for one TB per group. Preserve every requirement’s necessary conditions and expected behavior, then minimize the group count. Return requirement IDs and the arrangements needed to test them together.

## Input format

```jsonc
{
  "spec": "", // Complete specification text
  "rtl": {}, // File name → RTL with original line numbers
  "requirements": [], // Requirements: id, verbatim requirement, loc line ranges, trigger, expected result and reason
  "defines": [] // Optional compile definitions for this group
}
```

**Actual input**
{{input}}

## Rules
1. **Assign every input requirement.** Use the supplied IDs, normally once each. When one requirement needs separate groups, retain its ID in each and state the assigned conditions in `plan`; their union must cover the original checking scope.
2. **Group compatible test conditions.** Compare configuration, clocks/reset, state preparation and test procedures using the Spec and RTL. Share drivers, preparation and checking logic where all required conditions remain executable. Normal, boundary and exceptional scenarios may run in separate phases within one group; choose groups by these constraints rather than names, source locations or item counts.
3. **Preserve required state continuity.** Independent scenarios may reset or reconfigure between phases. State-retention, mode-transition and cross-cycle checks preserve the necessary history. Try reordering or state isolation before separating groups, and state the relevant preparation/order in `plan`.
4. **Distinguish instance settings from compilation settings.** Runtime settings may change within a group. Different instance parameters may use separate DUT instances in one build, with separate state and shared helpers. Package constants and global macros apply to the build; incompatible builds need separate groups. Use configurations supported by the Spec and RTL. A supplied `defines` is the complete command-line macro list, including other required macros; omit it to inherit the input list, or use `[]` for no command-line macros.
5. **Keep the plan focused on shared execution.** State necessary configuration, shared preparation, ordering/state isolation and conditions split across groups. Refer to requirements by ID; identify the real design outputs or states to observe and, when confirmed from the supplied RTL, their direct assignment/driver file and line for TB construction. Do not substitute requirement `loc` or gold locations, or trace recursive root causes; detailed stimuli and TB code belong in the TB construction step. Identify conflicting conditions or expectations by ID and retain them for Spec-based resolution. Before returning, check that every ID and its required conditions are assigned.

## Available tools
The program supplies the complete Spec and numbered RTL; return the grouping directly.

## Output format
Return only the following JSON array. Keep the field names and nesting unchanged; comments describe the required content.

```jsonc
[
  {
    "requirements": [], // nonempty array<string>; input requirement IDs assigned to this group
    "plan": "", // string; shared configuration, preparation, ordering, state isolation and condition splits
    "defines": [] // optional array<string>; complete compilation macros, or [] for none
  }
]
```
Every input requirement ID must be assigned at least once.

## Output example

```json
[
  {
    "requirements": ["R1", "R2", "R3", "R4", "R5", "R6"],
    "defines": [],
    "plan": "Compile with ASYNC disabled. R1-R4 share a DEPTH=8 instance, clock, driver, and reference queue. Run R1, R2, then R3, preserving the full state between R1 and R2; prepare a nonempty FIFO separately for R4. Use independent DEPTH=1 and DEPTH=3 instances for R5 and R6 in the same build, reusing the driver and reference-queue helpers while keeping each instance's state separate."
  },
  {
    "requirements": ["R7", "R8"],
    "defines": ["ASYNC=1"],
    "plan": "Compile with ASYNC enabled. R7 and R8 share a DEPTH=8 instance, independent read/write clocks, and a reference queue. Include R8's full-state read and synchronization interval within R7's transfer sequence, preserving FIFO and reference state throughout that interval. Use a separate group because the global ASYNC setting changes the interface and cannot coexist with the synchronous build."
  }
]
```

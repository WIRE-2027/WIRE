# Prompt 1: Group mismatch entries

## Role
You are an RTL functional verification engineer.

## Task
Group supplied mismatches that can share temporal analysis. Select an existing representative and preserve each member with its conditions, observations and CHECK mapping.

## Input format

```jsonc
{
  "spec": "", // Complete specification text
  "rtl": {}, // File name → RTL with original line numbers
  "requirements": [], // Requirements: id, verbatim requirement, loc line ranges, trigger, expected result and reason
  "entries": [], // Mismatch entries: id, test, signal, time, expected, actual and requirements
  "tests": [], // Executed testbenches, configurations and check records
  "actual_drivers": [] // Optional actual RTL drivers for observed signals
}
```

**Actual input**
{{input}}

## Rules
1. Assign every entry to exactly one group. Use an existing member as representative; keep uncertain relationships in separate groups.
2. Compare actual configuration, reset state, stimulus phase, timing obligations and related operations. A shared requirement or similar signal name alone is insufficient.
3. Group entries on a shared dependency chain when execution context and timing agree. Among supplied mismatches on that chain, select the furthest upstream entry whose dependency and timing are established; otherwise retain separate groups. A common line is a location clue, not a chain by itself. Preserve every member for its own explanation and candidate association.
4. Explain the shared temporal relationship briefly. tests contains complete code or code_from plus an exact code_diff; retain each member's observations and CHECK mapping.

## Available tools
This stage uses the supplied input. Return the groups directly.

## Output format
Return only the following JSON object. Keep the field names and nesting unchanged; comments describe the required content.

```jsonc
{
  "groups": [ // array<object>; every input mismatch entry appears exactly once
    {
      "representative": "", // string; copied exactly from a supplied entry ID
      "members": [], // nonempty array<string>; supplied entry IDs including representative
      "reason": "" // string; temporal and dependency reason for sharing analysis
    }
  ]
}
```
Invent no IDs, omit no IDs, and assign every supplied entry to exactly one group.

## Output example

```json
{
  "groups": [
    {
      "representative": "E1",
      "members": [
        "E1",
        "E2"
      ],
      "reason": "E1 observes count and E2 observes y=count from the same update and sampling phase."
    },
    {
      "representative": "E3",
      "members": [
        "E3"
      ],
      "reason": "E3 observes a separate reset-release condition."
    }
  ]
}
```

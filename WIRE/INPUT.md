# Input

A `case.json` describes a complete RTL compilation unit and its specification:

```json
{
  "root": ".",
  "spec": "spec.md",
  "target": "dut.sv",
  "top": "dut",
  "files": ["dut.sv"]
}
```

`root` is relative to the case file. Source and specification paths are relative to `root`. Include packages, headers and child modules in `files`. Optional `compile_files`, `include_dirs` and `defines` configure compilation; `model_files` selects the RTL shown to the model. Multi-file targets use `targets` and per-file `specs`.

Benchmark tasks supply the target files and their specifications. Full-design compilation also requires the corresponding project's packages, headers and child modules.

---
labels:
  - { value: in_scope, score: 1.0, description: "Only what the spec asks for, plus tests for it." }
  - { value: minor_extras, score: 0.6, description: "Small unrequested changes such as renames or formatting." }
  - { value: out_of_scope, score: 0.0, description: "Unrequested features, refactors or dependency changes." }
passing_score: 0.6
sampling_rate: 0.10
judge: { model: claude-haiku@vertex, fallback: [gemini-lite@vertex] }
applies_to: [builder]
---
Compare diff.patch with what feature.md and spec.md ask for. Tests for the requested change are in scope. Look for unrequested features, refactors, renames, formatting changes and dependency changes.

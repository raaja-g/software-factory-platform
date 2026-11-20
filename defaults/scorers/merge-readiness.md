---
labels:
  - { value: A, score: 1.0, description: "Could merge as is: correct, tested, in scope." }
  - { value: B, score: 0.7, description: "Needs small fixes before merging." }
  - { value: C, score: 0.0, description: "Wrong, untested, or out of scope." }
passing_score: 0.7
sampling_rate: 0.10
judge: { model: claude-haiku@vertex, fallback: [gemini-lite@vertex] }
applies_to: [builder]
---
Judge whether the change in diff.patch could be merged. Read feature.md and spec.md for what was asked, diff.patch for what was done, verify.md for the test results and the reviews and decision.md for what the reviewers said. Choose the label that fits best.

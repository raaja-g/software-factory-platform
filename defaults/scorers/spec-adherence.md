---
labels:
  - { value: all_met, score: 1.0, description: "Every acceptance criterion is met by the diff." }
  - { value: mostly_met, score: 0.5, description: "One criterion is missing or only partly met." }
  - { value: not_met, score: 0.0, description: "Several criteria are missing, or the diff does something else." }
passing_score: 1.0
sampling_rate: 0.10
judge: { model: claude-haiku@vertex, fallback: [gemini-lite@vertex] }
applies_to: [builder]
---
Check each acceptance criterion in spec.md against diff.patch and the test results in verify.md. Count a criterion as met only when the diff clearly implements it.

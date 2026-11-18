## Summary
A small todo app.

## Requirements
1. Add and list todos.
2. Mark a todo done.

## Out of scope
Accounts and sharing.

## Backlog
```yaml
- id: setup
  title: Set up the project
  feature: Create the project skeleton, a test command and CI.
  depends_on: []
  acceptance: ["`make test` runs and passes"]
- id: add
  title: Add todos
  feature: Add and list todos.
  depends_on: [setup]
  acceptance: ["POST /todos creates one", "GET /todos lists them"]
```

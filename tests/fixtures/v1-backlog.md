# v1 backlog: the four features replayed by the M7 acceptance run
- [ ] Bootstrap a minimal Python 3 project: `src/app/` package, pytest setup, a `make test` target, and a README explaining how to run tests
- [ ] Add a `todo` CLI (`python -m app.todo add|list|done <n>`) storing items in a JSON file in the user's home directory
- [ ] Add a tiny local web page (served by Python's stdlib http.server) that lists todos and lets you add and complete them
- [ ] Fix: browser form POSTs to the todo web page get 403 "cross-origin request refused". Cause: Referrer-Policy: no-referrer makes browsers send Origin: null on same-origin form POSTs (Fetch spec), which _check_origin rejects. Change Referrer-Policy to same-origin, keep rejecting Origin: null, and add a test that fails if the referrer policy would make browsers send Origin: null (ideally a real-browser test of add and done).

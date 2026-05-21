## Summary

## Verification

- [ ] `python3 -m unittest discover -s tests -p 'test_product_lifecycle.py'`
- [ ] `python3 -m unittest discover -s tests -p 'test_v2.py'`
- [ ] `python3 -m unittest discover -s tests -p 'test_runtime_hygiene.py'`
- [ ] If release/public docs changed: `python3 scripts/check_release_artifacts.py $(git ls-files)`

## Data Hygiene

- [ ] No `state/`, `artifacts/`, `config/`, logs, browser profiles, private URLs, thread IDs, tokens, or secrets are committed.

## UI Notes

- [ ] If this changes the browser/control-panel UI, note remaining rough edges or include a screenshot.

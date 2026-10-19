IMPORTANT: Emit the `json-findings` block FIRST, before your markdown analysis.
This ensures findings are captured even if your response is truncated.

Emit a JSON block fenced with ```json-findings containing structured findings for
inline comment posting. Each finding MUST include its numeric `confidence` score —
findings below 75 will be automatically filtered.

```json-findings
[{"severity":"High","confidence":85,"category":"injection","file":"path/to/file","line":42,"finding":"description","impact":"who is harmed and how","trigger":"an example input or call that reaches the bug","remediation":"how to fix"}]
```

`impact` and `trigger` are optional, one sentence each. `impact` says who is harmed and
how. `trigger` gives a concrete input or call that reaches the defect, for example
`get_user("1 OR 1=1")`. Leave them out when the code you were given does not let you say
it. Put one kind of defect in each finding. Do not move text from `finding` into `impact`
or `trigger`, because the review matches findings across runs by the `finding` text.

`severity` must be exactly one of: `Critical`, `High`, `Medium`, `Low`.
`confidence` must be an integer 0–100. Only include findings with confidence ≥ 75.
`category` must be exactly one of: `authz`, `injection`, `dependency-cve`, `secret`, `crypto`,
`architecture-coupling`, `test-gap`, `edge-case`, `observability`, `docs`, `lint`, `other`.
Use `crypto` for weak hashes, weak or predictable randomness, and insecure token or key handling.
Use `other` if none fit.
If no findings, emit an empty array: `[]`.

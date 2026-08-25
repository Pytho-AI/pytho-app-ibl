"""Parallel-DAG runtime for doctrinal product generation.

Currently INTSUM only. Other products fall through to the existing
operations-expert / product-writer dispatch flow.

Modules:
    dag_parser   — schema → list[Node] derivation (regex over guidance prose,
                   source-token vocabulary, header alias resolution).
    dag_executor — frontier-expansion async runner. Reads schema, parses to
                   nodes, executes wave-by-wave via asyncio.gather, merges
                   per-node payloads into the canonical structured_data shape,
                   validates, registers via state.with_artifact, renders.

Both modules are imported via `from runtime.dag_parser import ...` and
`from runtime.dag_executor import run_dag` — the docker-api container's
working directory is /app, so the package name resolves from there.
"""

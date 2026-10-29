# Pod Decision Service fixtures

Examples of the two calls the fast-ctv Prebid Server module (`pbs/fast-ctv`) makes to the Pod Decision Service, schema
version 1. The module's JUnit tests read them, and the Pod Decision Service's tests must accept and produce them, so
both sides of each call are tested against the same documents. See [docs/demand.md](../../../docs/demand.md#the-fast-ctv-module).

| File | Call | Direction |
|---|---|---|
| `policy-request.json` | `POST /policy` | module → service: the break, the session and the request headers it may use |
| `policy-response-contextual.json` | `POST /policy` | service → module: a contextual break with the simulator, content metadata and the viewer's IP |
| `policy-response-safe.json` | `POST /policy` | service → module: a probe break, house only |
| `select-request.json` | `POST /select` | module → service: the slots and every usable bid |
| `select-response.json` | `POST /select` | service → module: the pod in `sequence` order, one standalone per winner, extra trackers |

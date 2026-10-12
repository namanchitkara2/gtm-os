# Architecture

How GTM-OS fits together. Solid boxes are built and running locally; dashed boxes are
planned or not yet wired (see the status table in the README).

## System overview

```mermaid
flowchart LR
    subgraph User["User's machine"]
        Agent["Claude Code / Codex<br/>(user's own agent)"]
        Bridge["Local bridge<br/>apps/bridge"]
        LI["LinkedIn session<br/>(never leaves the machine)"]
        Bridge --- LI
    end

    subgraph Platform["GTM-OS platform"]
        CP["Control plane<br/>MCP server + API + run executor<br/>apps/control-plane"]
        Web["Dashboard (Vite + React)<br/>traces, people, bridge, pairing"]
        Schemas["Shared schemas<br/>packages/schemas"]
        Router["Provider router<br/>cache, then cheapest source<br/>packages/providers"]
        PG[("Postgres<br/>row-level security<br/>state + memory layer")]
        Runner["Graph runner<br/>apps/graph-runner"]:::planned
        Providers["Enrichment providers<br/>emails, phones"]:::planned
        Auth["Cognito behind OAuthProxy"]:::planned
    end

    Agent -- "MCP tools over HTTP" --> CP
    CP -- "WSS tunnel: one action at a time" --> Bridge
    CP <--> PG
    CP --> Router
    Router <--> PG
    Router -.-> Providers
    Web -- "/api/*" --> CP
    CP -.-> Runner
    CP -.-> Auth
    Schemas -. shared by .- CP
    Schemas -. shared by .- Bridge

    classDef planned stroke-dasharray: 5 5,fill:#f6f6f6,color:#555;
```

## A sourcing run

```mermaid
sequenceDiagram
    autonumber
    participant A as User's agent
    participant C as Control plane
    participant M as Memory (Postgres)
    participant B as Local bridge
    participant L as LinkedIn

    A->>C: define_target
    A->>C: start_run
    C->>M: recall (cache and prior decisions)
    alt cache hit
        M-->>C: known people, no LinkedIn action spent
    else cache miss
        C->>B: dispatch one action over WSS
        B->>L: search / read profile
        L-->>B: raw page text
        B-->>C: raw text
        C->>C: structure + dedupe key
        C->>M: write people, evidence, provenance
    end
    C->>M: record qualification decision with evidence ids
    A->>C: get_run / get_results
    C-->>A: ranked people with rationale
```

## Enrichment routing

```mermaid
flowchart TD
    Need["Field needed (email, phone, ...)"] --> Cache{"In cache<br/>within TTL?"}
    Cache -- yes --> Done["Use cached value"]
    Cache -- no --> Cheap["Try cheapest provider<br/>that could meet the confidence bar"]
    Cheap --> Enough{"Confidence<br/>met?"}
    Enough -- yes --> Store["Store value + provenance"]
    Enough -- no --> Next["Escalate to next provider"]
    Next --> Enough
    Store --> Ledger[("enrichment_ledger<br/>every attempt, incl. zero-cost ones")]
    Done --> Ledger
```

## Memory layer (the part that is the product)

```mermaid
flowchart LR
    People["people / companies<br/>(nodes)"] --- Edges["entity_edges<br/>typed relationships"]
    People --> Prov["Per-field provenance<br/>source, observed_at, confidence"]
    People --> Evidence["Evidence"]
    Evidence --> Decisions["Qualification decisions<br/>(a model verdict needs evidence)"]
    Evidence --> Claims["Message claims<br/>(NOT NULL evidence FK)"]
    People --> Snaps["Profile snapshots"] --> Signals["Signals from diffs"]
    People --> Inter["Interactions + objections"]
    Claims --> Outcomes["Outcomes per sent action"]
    Signals --> Redisc["rediscover:<br/>re-judge only people who changed"]
```

## Design rules

1. One LinkedIn action at a time per user, so the cache is the throughput.
2. LinkedIn returns raw text, so structuring happens server-side to enable dedupe and caching.
3. A write must never happen twice (see `BRIDGE.md`).
4. Provenance is a schema constraint, not a prompt instruction (ADR-007 in `architecture-decisions.md`).

Full decision records: `architecture-decisions.md`, `bottlenecks.md`, `BRIDGE.md`.

# Abhyas — User Flow

```mermaid
flowchart LR
    A["Open app"] --> B["Digital Twin 3D view<br/>watch live junction"]
    B --> C{"What does<br/>the user want?"}
    C --> D["Tweak signal/demand<br/>via dials or voice"]
    C --> E["Save / compare<br/>versions"]
    C --> F["Run validation or<br/>what-if analysis"]
    C --> G["Switch to CLI &<br/>Workflows dash"]
    D --> B
    E --> B
    F --> B
    G --> H["Run CLI commands /<br/>trigger workflows"]
    H --> B
```

## Notes

- **One home base**: the 3D Digital Twin view, always live.
- From there the user branches into four things: adjust the sim, manage versions, run analysis jobs, or hop to the CLI/Workflows dash — then comes back.

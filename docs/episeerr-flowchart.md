flowchart TD
    %% Section 1: Existing Sonarr Series
    A[Existing Series in Sonarr] --> B{Series has rule?}
    B -->|No| C[Episeerr ignores]
    B -->|Yes| D[User Watches Episode<br/>Tautulli/Jellyfin Webhook]
    D --> E[Apply Rule:<br/>• Get next episodes<br/>• Keep/Grace logic]
    E --> L[Grace Timer Cleanup<br/>Always Runs]

    %% Section 2: Request System
    F[Request Apps<br/>Jellyseerr/Overseerr] --> G{Has episeerr tag?}
    G -->|No| H[Normal Sonarr]
    G -->|Yes - episeerr_default| I[Add to Default Rule<br/>and Execute Immediately]
    G -->|Yes - episeerr_select| J[Episode Selection<br/>Interface]
    
    %% Section 3: Dormant Storage Management
    R[Series with Rules] --> S{Rule has<br/>dormant timer?}
    S -->|No| T[Protected Series]
    S -->|Yes| M{Storage below<br/>threshold?}
    M -->|No| N[No cleanup needed]
    M -->|Yes| Q[Remove Dormant Shows]

    %% Dry Run Checks
    L --> U{Dry Run?}
    U -->|Yes| V[Show what would<br/>be deleted - Grace]
    U -->|No| W[Actually delete<br/>expired grace episodes]

    Q --> X{Dry Run?}
    X -->|Yes| Y[Show what would<br/>be deleted - Dormant]
    X -->|No| Z[Actually delete<br/>dormant series]

    %% Styling
    classDef processBox fill:#d1fae5,stroke:#10b981,stroke-width:2px
    classDef webhookBox fill:#fef3c7,stroke:#f59e0b,stroke-width:2px
    classDef ruleBox fill:#dbeafe,stroke:#3b82f6,stroke-width:2px
    classDef tagBox fill:#fde68a,stroke:#d97706,stroke-width:2px
    classDef storageBox fill:#fce7f3,stroke:#ec4899,stroke-width:2px
    classDef conditionBox fill:#fed7d7,stroke:#ef4444,stroke-width:2px
    classDef dryRunBox fill:#e0e7ff,stroke:#6366f1,stroke-width:2px

    class A,C,H,N,T processBox
    class D,F webhookBox
    class E,I ruleBox
    class J tagBox
    class L,Q,R storageBox
    class B,G,M,S,U,X conditionBox
    class V,W,Y,Z dryRunBox

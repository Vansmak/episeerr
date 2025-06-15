<svg viewBox="0 0 1200 900" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <style>
      .title { font: bold 24px sans-serif; fill: #2563eb; }
      .section-title { font: bold 16px sans-serif; fill: #1e40af; }
      .text { font: 12px sans-serif; fill: #374151; }
      .small-text { font: 10px sans-serif; fill: #6b7280; }
      .webhook-box { fill: #fef3c7; stroke: #f59e0b; stroke-width: 2; }
      .rule-box { fill: #dbeafe; stroke: #3b82f6; stroke-width: 2; }
      .process-box { fill: #d1fae5; stroke: #10b981; stroke-width: 2; }
      .condition-diamond { fill: #fed7d7; stroke: #ef4444; stroke-width: 2; }
      .arrow { stroke: #4b5563; stroke-width: 2; fill: none; marker-end: url(#arrowhead); }
      .decision-arrow { stroke: #ef4444; stroke-width: 2; fill: none; marker-end: url(#arrowhead); }
      .tag-box { fill: #fde68a; stroke: #d97706; stroke-width: 2; }
      .storage-box { fill: #fce7f3; stroke: #ec4899; stroke-width: 2; }
    </style>
    <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
      <polygon points="0 0, 10 3.5, 0 7" fill="#4b5563" />
    </marker>
  </defs>

  <!-- Title -->
  <text x="600" y="30" text-anchor="middle" class="title">Episeerr Complete System Flow</text>

  <!-- Section 1: Existing Sonarr Series (Most Common) -->
  <text x="50" y="70" class="section-title">1. Existing Sonarr Series</text>
  
  <rect x="50" y="80" width="120" height="40" class="process-box" rx="5"/>
  <text x="110" y="95" text-anchor="middle" class="text">Existing Series</text>
  <text x="110" y="110" text-anchor="middle" class="text">in Sonarr</text>

  <!-- Rule Check -->
  <polygon points="220,100 270,80 320,100 270,120" class="condition-diamond"/>
  <text x="270" y="95" text-anchor="middle" class="small-text">Series has</text>
  <text x="270" y="105" text-anchor="middle" class="small-text">rule?</text>

  <!-- No Rule Path -->
  <rect x="370" y="60" width="100" height="30" class="process-box" rx="5"/>
  <text x="420" y="80" text-anchor="middle" class="text">Episeerr ignores</text>

  <!-- Has Rule - Viewing Trigger -->
  <rect x="200" y="160" width="140" height="40" class="webhook-box" rx="5"/>
  <text x="270" y="175" text-anchor="middle" class="text">User Watches Episode</text>
  <text x="270" y="190" text-anchor="middle" class="text">(Tautulli/Jellyfin)</text>

  <!-- Rule Application -->
  <rect x="200" y="220" width="140" height="60" class="rule-box" rx="5"/>
  <text x="270" y="240" text-anchor="middle" class="text">Apply Rule:</text>
  <text x="270" y="255" text-anchor="middle" class="text">• Get next episodes</text>
  <text x="270" y="270" text-anchor="middle" class="text">• Keep/Grace logic</text>

  <!-- Section 2: Request System -->
  <text x="550" y="70" class="section-title">2. Request System (New Series)</text>
  
  <rect x="550" y="80" width="120" height="40" class="webhook-box" rx="5"/>
  <text x="610" y="95" text-anchor="middle" class="text">Request Apps</text>
  <text x="610" y="110" text-anchor="middle" class="text">(Jellyseerr/Overseerr)</text>

  <!-- Tag Check -->
  <polygon points="720,100 770,80 820,100 770,120" class="condition-diamond"/>
  <text x="770" y="95" text-anchor="middle" class="small-text">Has episeerr</text>
  <text x="770" y="105" text-anchor="middle" class="small-text">tag?</text>

  <!-- No Tag -->
  <rect x="870" y="85" width="100" height="30" class="process-box" rx="5"/>
  <text x="920" y="105" text-anchor="middle" class="text">Normal Sonarr</text>

  <!-- episeerr_default -->
  <rect x="700" y="160" width="140" height="40" class="tag-box" rx="5"/>
  <text x="770" y="175" text-anchor="middle" class="text">episeerr_default:</text>
  <text x="770" y="190" text-anchor="middle" class="text">Add to Rule &amp; Execute</text>

  <!-- episeerr_select -->
  <rect x="700" y="220" width="140" height="60" class="tag-box" rx="5"/>
  <text x="770" y="240" text-anchor="middle" class="text">episeerr_select:</text>
  <text x="770" y="255" text-anchor="middle" class="text">Episode Selection</text>
  <text x="770" y="270" text-anchor="middle" class="text">Interface</text>

  <!-- Seer Request Removal (subtle note) -->
  <text x="770" y="300" text-anchor="middle" class="small-text">*Seer request removed</text>

  <!-- Section 3: Storage Management System -->
  <text x="50" y="350" class="section-title">3. Storage Management (Independent System)</text>
  
  <!-- Grace Cleanup (Always Runs) -->
  <rect x="50" y="370" width="140" height="50" class="storage-box" rx="5"/>
  <text x="120" y="390" text-anchor="middle" class="text">Grace Timer Cleanup</text>
  <text x="120" y="405" text-anchor="middle" class="text">(Always Runs)</text>

  <!-- Storage Gate Check -->
  <polygon points="250,395 320,375 390,395 320,415" class="condition-diamond"/>
  <text x="320" y="390" text-anchor="middle" class="small-text">Storage below</text>
  <text x="320" y="400" text-anchor="middle" class="small-text">threshold?</text>

  <!-- No Storage Issue -->
  <rect x="440" y="355" width="100" height="30" class="process-box" rx="5"/>
  <text x="490" y="375" text-anchor="middle" class="text">No cleanup needed</text>

  <!-- Dormant Check -->
  <polygon points="250,455 320,435 390,455 320,475" class="condition-diamond"/>
  <text x="320" y="450" text-anchor="middle" class="small-text">Rule has</text>
  <text x="320" y="460" text-anchor="middle" class="small-text">dormant timer?</text>

  <!-- Protected -->
  <rect x="440" y="440" width="100" height="30" class="rule-box" rx="5"/>
  <text x="490" y="460" text-anchor="middle" class="text">Protected Series</text>

  <!-- Dormant Cleanup -->
  <rect x="250" y="510" width="140" height="40" class="storage-box" rx="5"/>
  <text x="320" y="525" text-anchor="middle" class="text">Remove Dormant</text>
  <text x="320" y="540" text-anchor="middle" class="text">Shows</text>

  <!-- Rule Components Reference -->
  <rect x="50" y="580" width="540" height="100" class="rule-box" rx="5"/>
  <text x="320" y="605" text-anchor="middle" class="section-title">Rule Components (Linear Viewing Only)</text>
  <text x="70" y="630" class="text">• Get: episodes/seasons/all to prepare next (3 episodes, 1 season, all)</text>
  <text x="70" y="650" class="text">• Keep: episodes/seasons/all to retain after watching (2 episodes, 1 season, all)</text>
  <text x="70" y="670" class="text">• Grace: days to protect kept content after watching (7 days, null=forever)</text>

  <!-- Key Principles -->
  <rect x="620" y="350" width="320" height="240" class="rule-box" rx="5"/>
  <text x="780" y="375" text-anchor="middle" class="section-title">Key Principles</text>
  <text x="640" y="400" class="text">• No rule assigned = Episeerr does nothing</text>
  <text x="640" y="420" class="text">• One rule maximum per series</text>
  <text x="640" y="440" class="text">• Rules assume linear viewing progression</text>
  <text x="640" y="460" class="text">• Grace cleanup always runs (viewing-based)</text>
  <text x="640" y="480" class="text">• Dormant cleanup only when storage low</text>
  <text x="640" y="500" class="text">• Episode selection bypasses all rules</text>
  <text x="640" y="520" class="text">• Tags trigger automatic processing</text>
  <text x="640" y="540" class="text">• Storage gate prevents unnecessary cleanup</text>

  <!-- Control Notes -->
  <rect x="620" y="610" width="320" height="60" class="webhook-box" rx="5"/>
  <text x="780" y="635" text-anchor="middle" class="section-title">User Controls</text>
  <text x="640" y="655" class="text">• Rules are optional - create or skip</text>
  <text x="640" y="670" class="text">• Storage gate is optional - set threshold or skip</text>

  <!-- Arrows -->
  
  <!-- Existing series flow -->
  <line x1="170" y1="100" x2="220" y2="100" class="arrow"/>
  
  <!-- Rule check branches -->
  <line x1="320" y1="90" x2="370" y2="80" class="decision-arrow"/>
  <text x="340" y="78" class="small-text">No</text>
  
  <line x1="270" y1="120" x2="270" y2="160" class="decision-arrow"/>
  <text x="285" y="140" class="small-text">Yes</text>

  <!-- Viewing trigger to rule application -->
  <line x1="270" y1="200" x2="270" y2="220" class="arrow"/>

  <!-- Request system flow -->
  <line x1="670" y1="100" x2="720" y2="100" class="arrow"/>
  
  <!-- Tag check branches -->
  <line x1="820" y1="90" x2="870" y2="90" class="decision-arrow"/>
  <text x="840" y="85" class="small-text">No</text>
  
  <line x1="770" y1="120" x2="770" y2="160" class="decision-arrow"/>
  <text x="785" y="140" class="small-text">Yes</text>

  <!-- Storage management flow -->
  <line x1="190" y1="395" x2="250" y2="395" class="arrow"/>
  
  <!-- Storage gate branches -->
  <line x1="390" y1="385" x2="440" y2="370" class="decision-arrow"/>
  <text x="410" y="375" class="small-text">No</text>
  
  <line x1="320" y1="415" x2="320" y2="435" class="decision-arrow"/>
  <text x="335" y="425" class="small-text">Yes</text>

  <!-- Dormant check branches -->
  <line x1="390" y1="455" x2="440" y2="455" class="decision-arrow"/>
  <text x="410" y="450" class="small-text">No</text>
  
  <line x1="320" y1="475" x2="320" y2="510" class="decision-arrow"/>
  <text x="335" y="492" class="small-text">Yes</text>

</svg>
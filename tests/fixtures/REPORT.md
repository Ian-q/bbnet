# Breadboard netlist report

<!-- GENERATED — DO NOT EDIT. Source of truth: the island YAML in
     this directory; regenerate: bbnet report -->

## Island `demo-left` (full-830)

| Net | Members | Component edges |
| --- | --- | --- |
| `12V` | `50L` (lead 12V input) | — |
| `3V3` | `rail:top+` (wire→demo-left:40e, R1.b); `40L` (J1.VCC, wire→demo-left:rail:top+) | R1 resistor 10k ↔ `5V` |
| `5V` | `rail:bot+` (C2.a) | C2 electrolytic 47u ↔ `GND` |
| `5V` | `10L` (U1.1, R1.a) | R1 resistor 10k ↔ `3V3` |
| `GND` | `rail:bot-` (C2.b) | C2 electrolytic 47u ↔ `5V` |
| `GND` | `rail:top-` (wire→demo-left:41e, C1.b); `41L` (J1.GND, wire→demo-left:rail:top-) | C1 ceramic 100n ↔ `N$demo-left:12L` |
| `GND` | `11L` (U1.2) | — |
| `N$demo-left:10R` | `10R` (U1.12) | — |
| `N$demo-left:11R` | `11R` (U1.11) | — |
| `N$demo-left:12L` | `12L` (U1.3, C1.a) | C1 ceramic 100n ↔ `GND` |
| `N$demo-left:12R` | `12R` (U1.10) | — |
| `N$demo-left:13L` | `13L` (U1.4) | — |
| `N$demo-left:13R` | `13R` (U1.9) | — |
| `N$demo-left:14L` | `14L` (U1.5) | — |
| `N$demo-left:14R` | `14R` (U1.8) | — |
| `N$demo-left:15L` | `15L` (U1.6) | — |
| `N$demo-left:15R` | `15R` (U1.7, wire→demo-right:15a) | — |
| `N$demo-left:20L` | `20L` (wire→demo-left:24a); `24L` (wire→demo-left:20a) | — |
| `N$demo-left:30L` | `30L` (LK1.p1); `34L` (LK1.p2) | — |
| `N$demo-left:36L` | `36L` (Q1.G) | — |
| `N$demo-left:37L` | `37L` (Q1.D) | — |
| `N$demo-left:38L` | `38L` (Q1.S) | — |
| `N$demo-left:42L` | `42L` (J1.TX) | — |
| `N$demo-left:43L` | `43L` (J1.RX) | — |

## Island `demo-right` (half-400)

| Net | Members | Component edges |
| --- | --- | --- |
| `3V3` | `rail:top+` (R2.b) | R2 resistor 4k7 ↔ `N$demo-right:20L` |
| `3V3` | `8L` (U2.1) | — |
| `GND` | `rail:top-` (—) | — |
| `GND` | `25L` (lead bench GND) | — |
| `GND` | `8R` (U2.8) | — |
| `N$demo-left:15R` | `15L` (wire→demo-left:15j) | — |
| `N$demo-right:10L` | `10L` (U2.3) | — |
| `N$demo-right:10R` | `10R` (U2.6) | — |
| `N$demo-right:11L` | `11L` (U2.4) | — |
| `N$demo-right:11R` | `11R` (U2.5) | — |
| `N$demo-right:20L` | `20L` (R2.a) | R2 resistor 4k7 ↔ `3V3` |
| `N$demo-right:9L` | `9L` (U2.2) | — |
| `N$demo-right:9R` | `9R` (U2.7) | — |

## Check summary

- 0 error(s), 3 warning(s), 0 todo(s)

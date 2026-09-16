# Autonomous Partner Inference & Calibration Guide

When an operator, founder, or partner alliance manager wants to discover and qualify implementation partners, **never start by interrogating them with generic questions**, and **never jump blindly into web searches with vague keywords** (e.g. *"consulting firms"*, *"dev shops"*).

An intelligent agent harness must first inspect all available context—past session transcripts, agent memories, and the local repository/codebase—to autonomously infer the target product, technical stack, delivery requirements, and partner profile.

Only after this autonomous discovery phase should the harness perform a gap analysis and conduct a focused, 30-second interview for anything missing.

---

## 1. Autonomous Pre-Interview Discovery (History, Memory, & Repo First)

Before prompting the operator, the harness executes these autonomous discovery steps:

### A. Inspect Past Sessions, Transcripts, & Agent Memories
- **Transcripts**: Use current conversation and relevant history explicitly supplied by the user.
- **Search Patterns**: Grep for partner discussions, systems integrators, consultancies, target customers, and agency partnerships:
  ```bash
  grep -Ei "partner|integrator|agency|consulting|reseller|ecosystem|channel" <transcript_path>
  ```
- **Extract**: Prior partner names, preferred agency sizes, target customer segments, and partner tier expectations.

### B. Inspect Workspace Codebase & Architecture
- **Root Metadata**: Read `README.md`, `pyproject.toml`, `package.json`, or integration connectors to identify the exact technology being deployed or extended.
- **Integration Connectors & Client SDKs**:
  - Scan package dependencies, imports, and integration directories (`connectors/`, `providers/`, `adapters/`).
  - *Deduction Logic*:
    - If product integrates with Snowflake -> Target partners must have cloud data warehouse practices.
    - If product runs on Kubernetes / Helm -> Target partners must have cloud-native platform engineering practices.
    - If product is an AI/LLM framework -> Target partners must build custom AI/RAG solutions for clients.
- **Client Facing Guides**: Check `docs/` for integration manuals, quickstarts, and enterprise deployment blueprints.

---

## 2. The 6 Target Partner Signals: What They Mean & How to Gather Them

The harness must systematically resolve these 6 core signals:

### Signal 1: Target Ecosystem / Platform
- **What it means**: The core product, technology, or platform the partner will implement for their clients.
- **How to gather it**: Check product README, package manifests, and core API surfaces.
- **How to think about it**: If our product is an event streaming platform, partners must have deep Apache Kafka or messaging expertise.

### Signal 2: Required Adjacent Competencies
- **What it means**: The technical foundations and tools that partner engineers must already master (e.g. AWS, Terraform, Python, Kubernetes, dbt).
- **How to gather it**: Grep for dependencies, cloud providers, and supported runtime environments in the repo.

### Signal 3: Service Delivery Models
- **What it means**: What type of client engagements do we want the partner to execute? (e.g. Turnkey Systems Integration, Legacy Migration, Custom App Dev, Managed Services, or Architecture Advisory).
- **How to think about it**: A vendor seeking partners to offload enterprise migrations needs full-lifecycle SIs, not 2-person design studios.

### Signal 4: Target Client Segment & Scale
- **What it means**: What client size or industry does the partner typically serve? (e.g. Fortune 500 Enterprise, Mid-Market Scaleups, Regulated Fintech/Healthcare).
- **How to gather it**: Match with the product's ICP and pricing tier.
- **Gather the partner's own firmographics as values, not prose.** The funnel
  eliminates on these before it reads anything semantic, so each one has to be
  something a gate can compare rather than a sentence it would have to read:
  - `partner_size_min` / `partner_size_max` — the headcount band that can
    actually deliver this work. "Regional SI, 50-500 consultants" belongs in
    these two fields; `target_partner_tier` keeps the prose label.
  - `target_territories` — every country in scope, named as a country. Synonym
    matching already accepts a page that says "London" for "United Kingdom", so
    listing cities is unnecessary.
  - `target_industries` — the verticals they must serve. Leave this empty rather
    than guessing: no vertical leaves that gate *unknown*, which is honest, while
    a wrong one eliminates a good partner.

### Signal 5: Key Delivery Roles & Hierarchy
- **What it means**: The exact job titles of the engineers and consultants who design and deploy solutions for clients.
- **How to gather it**: Map the service model to standard professional services hierarchy:
  - Systems Integration -> `Solutions Architect`, `Senior Technical Consultant`, `Delivery Lead`
  - Cloud Migration -> `Cloud Migration Architect`, `Principal Infrastructure Consultant`
  - Practice Leadership -> `Practice Director`, `VP Professional Services`, `Alliances Lead`

### Signal 6: Anchor Partner Exemplars
- **What it means**: 2–3 existing high-performing partner agencies or ideal model firms (e.g. Slalom, Trace3, WWT, or specialized boutique consultancies).
- **How to gather it**: Check existing partner directories, past transcripts, or founder notes.

---

## 3. The Gap Analysis & Delta Determination

After running autonomous discovery, the harness evaluates each signal:

```
[Partner Signal Status Scorecard]
1. Target Ecosystem / Stack:     [ CONFIRMED | INFERRED | MISSING ]
2. Required Adjacent Skills:     [ CONFIRMED | INFERRED | MISSING ]
3. Service Delivery Model:       [ CONFIRMED | INFERRED | MISSING ]
4. Target Client Segment:        [ CONFIRMED | INFERRED | MISSING ]
5. Key Delivery Roles:           [ CONFIRMED | INFERRED | MISSING ]
6. Anchor Partner Exemplars:     [ CONFIRMED | INFERRED | MISSING ]
```

### The Invariant:
**NEVER ask the operator about signals that are already CONFIRMED or obvious from their repo and past sessions.**
Only interview for signals that are `MISSING` or ambiguous.

---

## 4. The Targeted Gap Interview

When any of the 6 signals remain missing, present the inferred foundation first, then ask only for the missing pieces:

> *"I scanned your codebase, dependencies, and past session history. Here is what I already inferred about your partner profile:*
> - **Target Platform / Ecosystem**: Apache Kafka & Distributed Event Streaming *(from README & connectors)*
> - **Required Adjacent Competencies**: Kubernetes, AWS, Python *(from dependencies)*
> - **Delivery Model**: Systems Integration & Enterprise Migration Services
> - **Key Delivery Roles**: Solutions Architect, Implementation Consultant
> 
> *To calibrate your 0–100 scoring rubric and discovery queries, I only need to clarify [1 or 2 missing details]:*
> 1. **Target Client Segment**: What client tier should these partners serve? (e.g. Fortune 500 Enterprise, Mid-Market B2B, or Regulated Fintech?)
> 2. **Anchor Partner Exemplars**: Are there 1 or 2 consultancies or SIs you consider the gold standard for this type of work? (e.g. Slalom, Trace3, or boutique data consultancies)?"*

---

## 5. Calibrated Profile Output (`ideal_partner_profile.json`)

Output the confirmed profile into `ideal_partner_profile.json`:

```json
{
  "ipp_profile": {
    "profile_name": "Kafka Streaming Implementation Partners",
    "version": "1.0.0",
    "target_ecosystem": "Apache Kafka",
    "partner_kind": "services",
    "partner_size_min": 50,
    "partner_size_max": 500,
    "target_territories": [
      "United States",
      "United Kingdom"
    ],
    "target_industries": [
      "fintech",
      "healthcare"
    ],
    "service_models": [
      "Systems Integration",
      "Enterprise Cloud Migration",
      "Managed Streaming Infrastructure"
    ],
    "required_adjacent_competencies": [
      "Kubernetes",
      "AWS EKS",
      "Python",
      "Terraform"
    ],
    "target_client_segment": [
      "Enterprise",
      "Financial Services",
      "High-Growth Tech"
    ],
    "target_partner_tier": "Regional to Enterprise Systems Integrator (50-500 consultants)",
    "key_delivery_roles": [
      "Solutions Architect",
      "Principal Streaming Consultant",
      "Technical Delivery Lead"
    ],
    "negative_exclusions": [
      "Pure SaaS Software Vendors",
      "Staffing / Recruiter Body Shops",
      "Generalist Web Design Agencies"
    ],
    "anchor_partners": [
      "slalom.com",
      "trace3.com"
    ]
  }
}
```

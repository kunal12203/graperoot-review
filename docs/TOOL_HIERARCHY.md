# GrapeRoot Pro — Tool Hierarchy

**67 MCP tools** organised in 3 tiers. Always start at Tier 1 and drill down only when you need more detail.

---

## How the nesting works

```
Tier 1 — Discovery
  graph_help(category?)
  └─ tells you which category + composite to call

Tier 2 — Composite  (one call = entire domain)
  graph_production_readiness()   ← THE master audit
  graph_security_audit()
  graph_resilience_audit()
  graph_ops_audit()
  graph_code_quality_audit()

Tier 3 — Individual  (67 specific tools)
  graph_scan_secrets()
  graph_goroutine_leaks()
  graph_cache_stampede()
  … 64 more
```

**Recommended flow for any investigation:**

```
1. graph_production_readiness()        → top-10 prioritised punch list
2. graph_help('ops')                   → see all ops tools
3. graph_goroutine_leaks()             → drill into specific area
```

---

## Tier 1 — Discovery

| Tool | What it does |
|------|-------------|
| `graph_help()` | List all 9 categories with descriptions |
| `graph_help('security')` | List every tool in the security category + composite name |

`graph_help` response shape:
```json
{
  "categories": {
    "security": {
      "description": "...",
      "tools": { "graph_scan_secrets": "...", ... },
      "composite": "graph_security_audit"
    }
  }
}
```

---

## Tier 2 — Composite Tools

Each composite runs its entire category in one call and returns a **prioritised punch list**.

### `graph_production_readiness()` ← start here
Runs ALL categories. Returns top-10 items ranked critical → high → medium → low.
```
Covers: secrets · resilience · ops · code_quality · idempotency · migrations
Output: top10_action_list, severity_breakdown, scores{resilience_score, health_score}
```

### `graph_security_audit()`
```
Covers: scan_secrets · sast_findings · log_secret_leakage · iac_security
        idempotency_gaps · unsafe_migrations
Output: top10_action_list, all_findings, categories_run
```

### `graph_resilience_audit()`
```
Covers: http_timeouts · retry_backoff · feature_flag_fallbacks · circuit_breakers
Output: top10_action_list, resilience_score, all_findings
```

### `graph_ops_audit()`
```
Covers: connection_pool_misconfigs · config_misconfigs · health_checks
        goroutine_leaks · cache_stampede · port_conflicts
        race_conditions · unused_env_vars · missing_env_vars
Output: top10_action_list, categories_run, all_findings
```

### `graph_code_quality_audit()`
```
Covers: n_plus_one · missing_pagination · missing_indexes
        resource_leaks · bean_collisions · missing_config_siblings
Output: top10_action_list, categories_run, all_findings
```

---

## Tier 3 — Individual Tools (67 total)

### routes — Find, list, and trace HTTP routes
| Tool | Purpose | Rule IDs |
|------|---------|----------|
| `graph_find_route(path)` | Which handler serves GET /payments? | — |
| `graph_list_routes(prefix?)` | All routes, filter by prefix or method | — |
| `graph_trace_event(event)` | Follow Kafka/SQS/Redis event end-to-end | — |
| `graph_who_publishes(topic)` | Who publishes to a given topic/queue? | — |

### models — Database models, ORM, schema
| Tool | Purpose | Rule IDs |
|------|---------|----------|
| `graph_db_models()` | All ORM models + relationships | — |
| `graph_tf_resources()` | Terraform resources by type | — |
| `graph_cdk_stacks()` | AWS CDK stacks (v1 + v2) | — |
| `graph_cfn_resources()` | CloudFormation resources | — |
| `graph_pulumi_resources()` | Pulumi resources | — |

### security — Secrets, SAST, vulnerabilities
| Tool | Purpose | Rule IDs |
|------|---------|----------|
| `graph_scan_secrets()` | Hardcoded credentials | SEC-* |
| `graph_sast_findings()` | Static analysis by language | PY*/JS*/GO*/JV* |
| `graph_log_secret_leakage()` | Secret vars in log calls (CWE-532) | LOG-001/002/003/004 |
| `graph_scan_vulnerabilities()` | OSV dependency CVEs | CVE-* |
| `graph_license_audit()` | License compliance | LIC-* |
| `graph_iac_security()` | Terraform/K8s/CDK misconfigs | TF*/K8S* |
| `graph_idempotency_gaps()` | MQ consumers missing ON CONFLICT | IDEM-001/002/003/004 |
| `graph_unsafe_migrations()` | ALTER TABLE without online DDL | MIGR-001/002/003/004 |
| ↑ **composite** | `graph_security_audit()` | runs all above |

### resilience — Timeouts, retries, circuit breakers
| Tool | Purpose | Rule IDs |
|------|---------|----------|
| `graph_http_timeouts()` | HTTP calls without timeout | RES-001 |
| `graph_retry_backoff()` | Retry with fixed sleep (not exponential) | RES-002 |
| `graph_feature_flag_fallbacks()` | Feature flags without fallback value | RES-003 |
| `graph_circuit_breakers()` | 3+ external calls, no circuit breaker | RES-004 |
| `graph_resilience_summary()` | Full resilience score (0-100) | all RES |
| ↑ **composite** | `graph_resilience_audit()` | runs all above |

### ops — Connection pools, health, goroutines, cache
| Tool | Purpose | Rule IDs |
|------|---------|----------|
| `graph_connection_pool_misconfigs()` | DB pool without max size | POOL-001/002/003/004/005 |
| `graph_config_misconfigs()` | PgBouncer/Kafka/HikariCP config files | CFG-001/002/003/004/005 |
| `graph_health_checks()` | Missing /health, no K8s readinessProbe | HLTH-001/002 |
| `graph_goroutine_leaks()` | cancel() not deferred, goroutine in loop | GLEAK-001/002/003 |
| `graph_cache_stampede()` | No singleflight, TTL=0, SET no expiry | STMP-001/002/003 |
| `graph_port_conflicts()` | Same port bound in multiple files | PORT-* |
| `graph_race_conditions()` | Shared mutable state in goroutines/threads | RACE-* |
| `graph_unused_env_vars()` | .env vars never referenced | ENV-001 |
| `graph_missing_env_vars()` | Vars in code missing from .env | ENV-002 |
| ↑ **composite** | `graph_ops_audit()` | runs all above |

### code_quality — N+1, pagination, leaks, debt
| Tool | Purpose | Rule IDs |
|------|---------|----------|
| `graph_n_plus_one()` | DB query inside a loop | NP1-* |
| `graph_missing_pagination()` | Query endpoints without LIMIT | PAG-* |
| `graph_missing_indexes()` | FK / filter fields without index | IDX-* |
| `graph_resource_leaks()` | Unclosed files, HTTP bodies, DB rows | LEAK-001/002/003/004/005 |
| `graph_debt_score()` | Technical debt score by file | — |
| `graph_dead_exports()` | Exported symbols never imported | — |
| ↑ **composite** | `graph_code_quality_audit()` | runs all above |

### observability — Tracing, metrics, alerting
| Tool | Purpose |
|------|---------|
| `graph_observability_summary()` | Full coverage score |
| `graph_otel_topology()` | OpenTelemetry trace topology |
| `graph_prometheus_alerts()` | Prometheus alert rules |
| `graph_sentry_coverage()` | Sentry error tracking coverage |
| `graph_datadog_coverage()` | Datadog APM + metrics coverage |
| `graph_apm_coverage()` | New Relic, Dynatrace, Honeycomb, Jaeger, Zipkin |
| `graph_newrelic_coverage()` | New Relic instrumentation |

### infra — CI/CD, IaC, Kubernetes
| Tool | Purpose |
|------|---------|
| `graph_ci_topology()` | CI/CD topology (12 systems) |
| `graph_ci_extended_summary()` | Travis, Drone, Bitbucket, ArgoCD, Tekton, Flux, TeamCity |
| `graph_iac_extended_summary()` | CDK, CFN, Pulumi, Bicep |
| `graph_kustomize_overlays()` | Kustomize overlay structure |
| `graph_lang_extended_summary()` | Elixir, Swift, Dart, Groovy breakdown |

### impact — Change impact, ownership
| Tool | Purpose |
|------|---------|
| `graph_pr_impact(files)` | What breaks if these files change? |
| `graph_who_owns(path)` | CODEOWNERS-aware file ownership |
| `graph_explain_path(a, b)` | Plain-English: how do A and B connect? |
| `graph_test_coverage()` | Test coverage by scope |
| `graph_diff(commit)` | What changed across services since commit? |
| `graph_version_audit()` | Dependency version conflicts |

---

## Punch list format

Every composite tool returns `top10_action_list` — a ranked list ready for a fix queue:

```json
[
  {
    "rank": 1,
    "severity": "critical",
    "rule_id": "CFG-003",
    "file": "config/kafka.properties",
    "line": 3,
    "action": "Set enable.auto.commit=false and call commitSync() after processing"
  },
  {
    "rank": 2,
    "severity": "high",
    "rule_id": "GLEAK-001",
    "file": "server/handler.go",
    "line": 12,
    "action": "Add defer cancel() immediately after context.WithCancel call"
  }
]
```

---

## Rule ID namespace

| Prefix | Domain | Phase |
|--------|--------|-------|
| `SEC-` | Hardcoded secrets | 5 |
| `SAST-` | Static analysis | 5 |
| `LOG-` | Secret in log (CWE-532) | 33 |
| `TF/K8S` | IaC misconfigs | 5 |
| `IDEM-` | Idempotency | 23 |
| `MIGR-` | Unsafe migrations | 25 |
| `RES-` | Resilience (timeouts/retries) | 22 |
| `POOL-` | Connection pool | 30 |
| `CFG-` | Config file (PgBouncer/Kafka/Hikari) | 34 |
| `HLTH-` | Health checks | 30 |
| `GLEAK-` | Goroutine/context leaks | 31 |
| `STMP-` | Cache stampede | 32 |
| `LEAK-` | Resource leaks | 24 |
| `NP1-` | N+1 queries | 8 |
| `PAG-` | Missing pagination | 8 |
| `IDX-` | Missing indexes | 9 |
| `PORT-` | Port conflicts | 13 |
| `RACE-` | Race conditions | 14 |
| `ENV-` | Env var issues | 11/12 |

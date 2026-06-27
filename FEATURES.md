# GrapeRoot Pro — Feature List

> **Implementation Status**: Phases 1–21 fully implemented in `graph_builder_v6.2.py` + `mcp_graph_server_v7.5.py`
> - Phase 1: Language Expansion (Java, Kotlin, Ruby, SQL, Shell, Scala, C#, Prisma) — `graph_builder_parsers.py`, `graph_builder_prisma.py`
> - Phase 2: Framework Route Detection (20 frameworks) — `graph_builder_routes.py` + 29 MCP tools in `mcp_tools_integration.py`
> - Phase 3: Service Graph (Kafka/SQS/Redis/RabbitMQ/NATS/EventBridge + Proto/OpenAPI/GraphQL/AsyncAPI) — `graph_builder_service_graph.py`
> - Phase 4: Infrastructure Parsing (Terraform, K8s, Helm, Kustomize, CI/CD) — `graph_builder_infra.py`
> - Phase 5: Security & Compliance (SAST, secrets, OSV, SBOM, debt score) — `security.py`
> - Phase 6: Structural Bug Detection (bean collisions, config parity, Kafka group collisions) — `structural_bugs.py`
> - Phase 7: Multi-Service & Advanced Tools (test coverage, PR impact, snapshots, federated search) — `advanced_tools.py`
> - Phase 8: Observability (OTel, Prometheus, Grafana, log patterns) — `observability.py`
> - Phase 9: ORM Detection (TypeORM, Sequelize, GORM, Drizzle, Mongoose, SQLAlchemy)
> - Phase 10: Framework Expansion (Fiber, Koa, Ktor, Quarkus, Rocket, Sinatra, Grape, Beego + CircleCI, Buildkite)
> - Phase 11: Security Expansion (21 new secret patterns, SAST for C#/Ruby/PHP/Kotlin, IaC misconfig scanner) — `security.py`
> - Phase 12: MQ Expansion (Celery, Azure Service Bus, GCP Pub/Sub, Temporal, Sidekiq/Resque)
> - Phase 13: Structural Checks (N+1 risk, missing pagination, missing indexes) — `structural_bugs.py`
> - Phase 14: Sentry + Datadog Observability Coverage — `observability.py`
> - Phase 15: IaC Extended (AWS CDK v1/v2, CloudFormation, Pulumi, Azure Bicep/ARM) — `graph_builder_iac_extended.py`
> - Phase 16: CI/CD Extended (Travis, Drone 0.x/1.x, Bitbucket Pipelines, ArgoCD, Tekton, Flux, TeamCity) — `graph_builder_ci_extended.py`
> - Phase 17: ORM Extended (EF Core, Dapper, Ent, Tortoise, SQLModel, Exposed, Knex, Kysely, Objection) — `graph_builder_orm.py`
> - Phase 18: Routes Extended (Starlette, Litestar, aiohttp, Django Ninja, Sanic, Tornado, GraphQL SDL, Apollo, Strawberry, Graphene, Feathers, AdonisJS, Elysia, Symfony, Play Framework, Buffalo) — `graph_builder_routes.py`
> - Phase 19: Observability Extended (New Relic, Dynatrace, Honeycomb, AlertManager, Loki, Jaeger, Zipkin) — `observability.py`
> - Phase 20: Structural Extended (unused env vars, missing env vars, port conflicts, race conditions) — `structural_bugs.py`
> - Phase 21: Language Extended (Elixir/Phoenix, Swift/Vapor, Dart/Flutter, Groovy/Gradle) — `graph_builder_lang_extended.py`

## Codebase Intelligence
- Instant symbol navigation — find any function, class, or variable across your entire project
- Cross-file impact analysis — know exactly what breaks before you change anything
- Dead export detection — find unused code automatically
- Circular dependency detection — within and across services
- BM25 semantic search across your full codebase

## Service Graph
- **API contracts** — Proto, OpenAPI/Swagger, GraphQL, AsyncAPI schemas parsed automatically
- **gRPC call graph** — which services call which, detected from code (Go, Python, Java)
- **HTTP internal calls** — traces `fetch`, `axios`, `requests.get` to destination services
- **Event/message topology** — Kafka, SQS, SNS, Redis, RabbitMQ, NATS, EventBridge mapped automatically
- **Route registry** — every HTTP endpoint across all services, searchable by method + path
- **Docker Compose** — container dependency graph from `docker-compose.yml`
- **Kubernetes manifests** — Deployments, Services, Ingress routes parsed into the graph

## Multi-Service Support
- **Multi-shard graphs** — each microservice gets its own shard, no data loss when switching projects
- **Cross-project edges** — connect shards and traverse the full system graph in one query
- **Federated search** — one query searches across all connected services simultaneously
- **Background scanning** — large services scan without blocking your work
- **Package-level granularity** — monorepos split into per-package shards automatically

## Developer Tools
- `graph_who_owns(file)` — CODEOWNERS-aware file ownership lookup
- `graph_find_route(POST, /payments)` — instantly find which service handles any endpoint
- `graph_trace_event(topic)` — follow an event chain through your entire system
- `graph_explain_path(A, B)` — plain-English explanation of how two components connect
- `graph_system_health()` — one-shot audit: orphan topics, cycles, dead contracts, dead exports
- `graph_version_audit()` — dependency version conflicts across all connected services
- `graph_diff(since)` — what changed across all services since a given timestamp
- `graph_impact(file)` — what breaks if you change this file, including contract warnings

## SQL & Shell (Phase 5)

### SQL DDL
`.sql` files parsed for schema definitions:
- `CREATE TABLE` → `sql_table` nodes with column lists
- `ALTER TABLE` → `alters_table` edges (migration tracking)
- Flyway `V1__init.sql` convention → `migration_version` field per table
- Alembic `Revision ID:` comment extracted

### Bash / Shell Scripts
`.sh`, `.bash`, `.zsh` files parsed for function definitions:
- `function name()` and `name()` syntaxes both detected
- `source ./lib/utils.sh` and `. ./lib/common.sh` → `sources` dependency edges

## Languages Supported
| Language | Coverage |
|----------|----------|
| TypeScript / JavaScript | AST-level (tree-sitter) + regex fallback |
| Python | Full symbol extraction, Django/SQLAlchemy ORM |
| Go | Full symbol + route extraction |
| Rust | Symbol + Axum/Actix route extraction |
| Java / Kotlin | Spring routes, gRPC stubs, JPA @Entity, annotations |
| C# | Class, interface, record, method extraction |
| PHP | Laravel routes, class/function extraction |
| Ruby | Rails routes, ActiveRecord models |
| Scala | Basic symbol extraction |
| SQL | CREATE TABLE / ALTER TABLE DDL, Flyway/Alembic migration versions |
| Bash / Shell | Function definitions, source dependencies |
| HCL / Terraform | Resource, module, data source extraction |
| Prisma | Schema model + relation extraction |

## Frameworks & Stacks
| Framework | What's detected |
|-----------|----------------|
| Express / Fastify | Routes |
| NestJS | `@Controller` + `@Get`/`@Post` routes with prefix combining |
| Hono | Routes |
| FastAPI / Flask | Routes |
| Django | `path()` / `re_path()` routes |
| Gin / Gorilla Mux | Routes |
| Spring Boot | `@GetMapping` / `@RequestMapping` routes |
| ASP.NET Core | `[HttpGet]` / `[Route]` routes |
| Laravel | `Route::get()` routes |
| Axum / Actix-web | Routes |
| Rails | Route definitions |
| Docker Compose | Container graph |
| Kubernetes | Workloads, services, ingress |
| Proto / gRPC | Contract nodes + call edges |
| OpenAPI / Swagger | Contract nodes |
| GraphQL | Schema contract nodes |
| AsyncAPI | Event channel contract nodes |
| Terraform / OpenTofu | Infrastructure resource graph |
| Helm | Chart + dependency graph |
| Kustomize | Overlay → base, resource, patch, component, image edges |
| GitHub Actions | Workflow + job dependency graph |
| GitLab CI | Stage + job graph |
| Jenkins | Pipeline stages, shared library, credential refs |
| Azure Pipelines | Job + stage dependency graph |
| Ansible | Playbook + task + role dependency graph |
| Next.js (App + Pages) | File-based route extraction |
| Remix | Loader/action route extraction |
| SvelteKit | +server.ts route extraction |
| Nuxt | Pages + server API routes |
| tRPC | Procedure + router graph |

## Structural Bug Detection (v7.6)

Automatically flags production-class bugs during every scan — no hardcoded service names, framework constants, or project-specific knowledge.

| Detection | What it finds | Languages |
|-----------|--------------|-----------|
| **Bean name collisions** | Two or more Spring `@Service`/`@Component` beans with the same canonical name across modules — causes wrong injection or context failure at startup | Java, Kotlin |
| **Missing config siblings** | Kafka producer/consumer config with `KEY_SERIALIZER` set but `VALUE_SERIALIZER` absent (and vice versa); `CLIENT_ID_CONFIG` set without `GROUP_ID_CONFIG` | Java, Kotlin, Python, TS, Go |
| **Duplicate config keys** | Same config constant set multiple times in one file — typically a copy-paste error overwriting a prior value | Java, Kotlin, Python, TS, Go |
| **Kafka consumer group collisions** | Two or more services sharing the same `groupId` on the same topic — Kafka splits partitions between them, silently dropping ~50% of messages per service | All |

Results returned in `graph_system_health()` and via `result['bean_collisions']`, `result['config_anomalies']`, `result['kafka_group_collisions']`.

## Security & Compliance (Phase 1)

### Secret Scanning
`graph_scan_secrets()` — finds hardcoded secrets across all code files.

| Pattern | Examples |
|---------|---------|
| AWS Access Keys | `AKIA...` — 20-char key format |
| AWS Secret Keys | `AWS_SECRET_KEY = "..."` |
| GitHub Tokens | `ghp_`, `gho_`, `ghs_` prefixes |
| Private Keys | `-----BEGIN RSA PRIVATE KEY-----` |
| Connection Strings | `postgresql://user:pass@host` |
| Stripe Keys | `sk_live_...`, `sk_test_...` |
| Google API Keys | `AIza...` |
| Slack Tokens | `xox[baprs]-...` |
| Generic passwords/API keys | High-entropy values only — ignores placeholders |

Skips test files, fixture directories, and placeholder values (`changeme`, `password`, etc.).

### SAST — Static Application Security Testing
`graph_sast_findings()` — pattern-based security analysis, no dataflow required.

| Rule | What it detects | Severity |
|------|----------------|----------|
| SAST-001 | SQL query built with string concat / f-string / `%` format | HIGH |
| SAST-002 | `subprocess` called with `shell=True` | HIGH |
| SAST-003 | `os.system()` / `os.popen()` | HIGH |
| SAST-004 | `eval()` / `exec()` with dynamic (non-literal) input | HIGH |
| SAST-005 | `pickle.loads()` unsafe deserialization | HIGH |
| SAST-006 | `yaml.load()` without safe Loader | HIGH |
| SAST-007 | MD5 / SHA-1 used for password or integrity check | MEDIUM |
| SAST-008 | `Math.random()` / `random.choice()` used for security tokens | MEDIUM |
| SAST-009 | File path constructed from request parameters | HIGH |
| SAST-010 | Hardcoded non-loopback IP address | MEDIUM |
| SAST-011 | Flask `app.run(debug=True)` | MEDIUM |
| SAST-012 | Django `DEBUG = True` in settings | MEDIUM |
| SAST-013 | `xml.etree.ElementTree` XXE vulnerability | HIGH |
| SAST-014 | Cookie set without `httponly=True` | MEDIUM |

Test files excluded from skip_if_test rules. Filter by `severity='HIGH'`, `category='injection'`, or `rule_id='SAST-001'`.

### Environment Parity
`graph_env_parity()` — detects configuration drift across Docker Compose, Kubernetes, Helm, and Terraform.

| Drift type | What it flags |
|------------|--------------|
| `image_tag_mismatch` | Same service, different image tags between Compose and K8s |
| `image_name_mismatch` | Different image names for the same service |
| `port_mismatch` | Service exposes different ports in Compose vs K8s containerPort |
| `missing_in_kubernetes` | Service in Compose with no matching K8s Deployment |
| `missing_in_compose` | K8s Deployment with no matching Compose service |

Service names normalised (hyphens and underscores treated as equivalent).

### Vulnerability Scanning
`graph_scan_vulnerabilities()` — scans all pinned dependencies against the [OSV.dev](https://osv.dev) database.

- **No API key required** — uses the free OSV.dev batch API
- Reads from: `package-lock.json`, `requirements.txt`, `go.mod`/`go.sum`, `pom.xml`, `build.gradle`, `Cargo.lock`, `Gemfile.lock`, `composer.lock`, `pyproject.toml`
- Only pinned/exact versions checked (version ranges excluded — use lock files for best coverage)
- Results sorted by severity: CRITICAL → HIGH → MEDIUM → LOW
- Filter by `ecosystem='npm'`, `severity='CRITICAL'`, or `file_prefix='services/payments'`

| Ecosystem | Manifest files |
|-----------|---------------|
| npm | package-lock.json |
| PyPI | requirements.txt, Pipfile.lock, pyproject.toml |
| Go | go.mod, go.sum |
| Maven | pom.xml, build.gradle |
| crates.io | Cargo.lock |
| RubyGems | Gemfile.lock |
| Packagist | composer.lock |

### License Compliance
`graph_license_audit()` — parses `package.json`, `pom.xml`, `build.gradle`, `go.mod` for copyleft licenses.
Flags GPL, LGPL, AGPL, MPL-2.0, EUPL with risk levels: `copyleft` | `unknown` | `permissive`.

## CI/CD Pipeline Topology (Phase 1)
`graph_ci_topology()` — maps your entire deploy pipeline into the graph.

| System | What's extracted |
|--------|----------------|
| GitHub Actions | Workflows, jobs, `needs:` dependencies, `secrets.*` refs, environments |
| GitLab CI | Stages, jobs, `needs:` deps, masked variable references |
| Jenkins | Pipeline stages, `credentialsId` refs, `@Library` shared libraries |

CI/CD nodes link to secret nodes, environment nodes, and service nodes in the same graph.

## API Breaking Change Detection (Phase 1)
- `graph_snapshot()` — save current route/contract state before a refactor
- `graph_api_diff(snapshot)` — compare to snapshot, flag: removed routes, removed contracts, new additions

## Automated Dead Code Removal (v7.6)

`graph_fix(fix_type="dead_methods")` — finds and removes dead exported methods with build-safe guards.

| Guard | What it prevents |
|-------|-----------------|
| `@Override` methods | Polymorphic framework dispatch |
| Spring/JAX-RS annotations | Framework-wired endpoints |
| Interface dispatch targets | Methods called via interface references |
| Factory methods (`of`, `create`, `builder`, etc.) | Generic/fluent call patterns |
| Constructor methods | Same name as class |
| Test files | Never modifies test code |

Multi-method removal sorts descending by line number to prevent stale-index errors.
Tested on production Java repos (resilience4j, eureka) — 100% compile rate after guards.

## Infrastructure as Code (Phase 2)

## IaC Security Scanning (Phase 11)

`graph_iac_security()` — scans Terraform, Kubernetes manifests, and Dockerfiles for security misconfigurations. Filter by `severity_min='low'|'medium'|'high'|'critical'`.

| Rule | What it detects | Severity |
|------|----------------|---------|
| TF001 | S3 bucket with public ACL | HIGH |
| TF002 | Security group ingress open to `0.0.0.0/0` | HIGH |
| TF003 | Security group egress open to `0.0.0.0/0` | MEDIUM |
| TF004 | RDS instance with `publicly_accessible = true` | HIGH |
| TF005 | S3 bucket missing versioning | MEDIUM |
| TF006 | EKS cluster with public endpoint enabled | HIGH |
| TF007 | IAM policy with overly permissive `*` actions | HIGH |
| TF008 | KMS key without key rotation enabled | MEDIUM |
| K8S001 | Container running without a security context | MEDIUM |
| K8S002 | Container with `allowPrivilegeEscalation: true` | HIGH |
| K8S003 | Container running as root (`runAsNonRoot: false`) | HIGH |
| DOCKER001 | Dockerfile `USER root` or no `USER` directive | HIGH |
| DOCKER002 | Dockerfile `FROM` using `:latest` tag | MEDIUM |

### Terraform / OpenTofu
`graph_tf_resources()` — list all cloud infrastructure resources from `.tf` and `.hcl` files.

| Resource Category | Examples |
|------------------|---------|
| AWS compute | `aws_lambda_function`, `aws_ecs_service`, `aws_eks_cluster` |
| AWS storage | `aws_s3_bucket`, `aws_dynamodb_table`, `aws_rds_cluster` |
| AWS messaging | `aws_sqs_queue`, `aws_msk_cluster`, `aws_secretsmanager_secret` |
| Google Cloud | `google_sql_database_instance`, `google_container_cluster` |
| Azure | `azurerm_sql_server`, `azurerm_kubernetes_cluster` |
| Kubernetes | `kubernetes_deployment`, `kubernetes_service`, `helm_release` |
| Modules | Any `module {}` block with `source` reference |

### Helm Charts
Helm `Chart.yaml` parsed into `helm_chart` nodes with `chart_depends_on` edges to dependencies.

### Kustomize Overlays
`graph_kustomize_overlays()` — maps your full GitOps overlay tree from `kustomization.yaml` files.

Each overlay records `namespace`, `namePrefix`, `nameSuffix` and emits typed edges:

| Edge type | What it represents |
|-----------|-------------------|
| `kustomize_base` | `bases:` or directory `resources:` — points to a base overlay |
| `kustomize_resource` | Individual manifest YAML file referenced in `resources:` |
| `kustomize_patch` | `patches:` / `patchesStrategicMerge:` patch file |
| `kustomize_component` | `components:` reusable component reference |
| `kustomize_image` | `images:` image override with `newTag` / `newName` |

Filter by `namespace='production'` or `base_filter='../../base'` to narrow results.

## Database Schema (Phase 2)
`graph_db_models()` — lists all ORM-defined tables/models across the codebase.

| ORM | Detection | Languages |
|-----|-----------|-----------|
| Django | `class Model(models.Model)` | Python |
| SQLAlchemy | `class Model(Base)`, `mapped_column`, `Column` | Python |
| Prisma | `model {}` blocks in `.prisma` schema | TypeScript / any |
| JPA / Hibernate | `@Entity` + `@Table` annotations | Java, Kotlin |
| ActiveRecord | `class Model < ApplicationRecord` + `belongs_to`/`has_many` | Ruby |

## ORM Support (Phase 9)

`graph_db_models()` extended to detect six additional ORM patterns across TypeScript, JavaScript, Go, and Python.

| ORM | Detection | Language |
|-----|-----------|----------|
| TypeORM | `@Entity`, `@Column`, `@ManyToOne` relations | TypeScript |
| Sequelize | `class extends Model`, `Model.init()` | JavaScript/TypeScript |
| GORM | struct with `gorm.io/gorm` import + gorm tags | Go |
| Drizzle ORM | `pgTable()`, `mysqlTable()`, `sqliteTable()` | TypeScript |
| Mongoose | `new mongoose.Schema()` + `mongoose.model()` | JavaScript/TypeScript |
| SQLAlchemy | class extends `Base` with `__tablename__` | Python |

## Modern Frontend Routes (Phase 2)

All routes merged into `route_registry` — queryable with `graph_find_route()`.

| Framework | Route files | Methods detected |
|-----------|-------------|-----------------|
| Next.js App Router | `app/**/page.tsx` | GET (page), GET/POST/PUT/DELETE (API routes) |
| Next.js Pages Router | `pages/**/` | ANY (handler export) |
| Remix | `app/routes/**` | loader (GET), action (POST/PUT/DELETE) |
| SvelteKit | `src/routes/**+server.ts` | GET/POST/PUT/PATCH/DELETE, load, actions |
| Nuxt | `pages/**/*.vue`, `server/api/**` | GET/POST (server routes) |

## Compliance & Quality (Phase 5)

### SBOM Generation
`graph_sbom()` — generates a Software Bill of Materials from the graph.

- `format='summary'` — returns all dependencies in the response (default)
- `format='cyclonedx'` — writes a CycloneDX 1.4 JSON file to disk

Covers: manifest-declared packages (package.json, pom.xml, go.mod, etc.) + infrastructure dependencies detected from code. Meets US EO 14028 and EU Cyber Resilience Act requirements.

### Technical Debt Scoring
`graph_debt_score()` — aggregate debt score (0–100, grade A–F) per codebase or module.

| Signal | Weight | What it measures |
|--------|--------|-----------------|
| Secret findings | 30% | Hardcoded credentials committed to code |
| Config anomalies | 20% | Missing config siblings, duplicate keys |
| Dead exports | 15% | Unused exported code accumulating |
| Uncovered exports | 15% | Public API with no test coverage |
| Bean collisions | 10% | Spring DI conflicts |
| License issues | 10% | Copyleft/unknown dependency licenses |

Use `file_prefix='src/payments'` for per-service debt reports.

## CI/CD Expansion (Phase 5)

### Azure Pipelines
`azure-pipelines.yml` parsed into the graph — jobs, stage dependencies, and Azure KeyVault secret references (`$(MY_KEYVAULT_SECRET)`).

### Ansible Automation
`playbook.yml` and `roles/*/tasks/main.yml` parsed into the graph.
- **ansible_play** nodes: name, hosts pattern, `become` flag
- **ansible_task** nodes: task name, Ansible module used (`apt`, `copy`, `service`, etc.)
- **uses_role** edges: play → role dependency
- **has_task** edges: play → task linkage

## Observability (Phase 4)

### OpenTelemetry Collector Topology
`graph_otel_topology()` — maps your full telemetry pipeline from config files.

Parses `otel-collector-config.yaml` into a pipeline graph:
- **Receivers** — `otlp`, `prometheus`, `jaeger`, `zipkin`, etc.
- **Processors** — `batch`, `memory_limiter`, `filter`, `transform`, etc.
- **Exporters** — `datadog`, `otlp/jaeger`, `prometheus`, `loki`, `tempo`, etc.
- **Pipelines** — traces / metrics / logs with which components connect

Filter by `signal_type='traces'`, `'metrics'`, or `'logs'`.

### Prometheus Alert Rules
`graph_prometheus_alerts()` — lists all alerting and recording rules.

Parses `prometheus.rules.yaml`, `alerting_rules.yaml`, `recording_rules.yaml`:
- Alert name, severity label, PromQL expression
- Filter by `severity='critical'` or `group='api-alerts'`

## Sentry + Datadog Coverage (Phase 14)

`graph_sentry_coverage()` — scans every `.py`, `.ts`, `.js`, `.go`, `.java`, `.kt` file for Sentry SDK usage.

| Detection | Languages |
|-----------|-----------|
| `sentry_sdk.init(dsn=...)` | Python |
| `sentry_sdk.capture_exception()` / `capture_message()` | Python |
| `@sentry_sdk.trace` decorator | Python |
| `with sentry_sdk.start_transaction(...)` | Python |
| `Sentry.init({dsn: '...'})` | TypeScript/JavaScript |
| `Sentry.captureException()` / `captureMessage()` | TypeScript/JavaScript |
| `Sentry.startTransaction({name: '...'})` | TypeScript/JavaScript |
| `withSentryConfig(...)` in `next.config.js` | TypeScript/JavaScript |
| `sentry.Init(...)` / `sentry.CaptureException()` / `sentry.StartSpan()` | Go |
| `Sentry.init(options ->` / `Sentry.captureException()` | Java/Kotlin |

Returns: `total_files_scanned`, `files_with_sentry`, `sentry_calls` (file + line + kind), `coverage_pct`.

`graph_datadog_coverage()` — scans for Datadog APM tracing SDK usage.

| Detection | Languages |
|-----------|-----------|
| `tracer.trace('operation')` / `tracer.configure()` | Python (ddtrace) |
| `@tracer.wrap(name='...')` decorator | Python (ddtrace) |
| `DD_AGENT_HOST` / `DD_SERVICE` / `DD_ENV` env var references | Python |
| `DogStatsD` / `statsd.increment()` | Python |
| `tracer.init({service: '...'})` | TypeScript/JavaScript (dd-trace) |
| `tracer.startSpan()` / `tracer.trace()` | TypeScript/JavaScript |
| `tracer.Start(tracer.WithService("..."))` | Go |
| `tracer.StartSpan()` / `tracer.StartSpanFromContext()` | Go |

Returns: `total_files_scanned`, `files_with_datadog`, `datadog_spans` (file + line + operation), `coverage_pct`.

`find_sentry_configs()` and `find_datadog_configs()` walk the project for `sentry.properties`, `sentry.yml`, `datadog.yaml`, `ddconfig.yaml`, and any source file with an init call.

## Developer Workflow (Phase 3)

### Static Test Coverage Analysis
`graph_test_coverage()` — finds exported symbols that are never imported or called from test files.

- No test runner required — pure static graph analysis
- Returns overall `coverage_pct`, broken down by `symbol_type`
- Highlights `api_route`, `model`, `use_case` symbols with zero test reach
- Scope to a module with `file_prefix=`

### PR Blast Radius Preview
`graph_pr_impact(changed_files)` — traces the impact of a PR before it merges.

Given a list of changed file paths, BFS traces reverse import/call/gRPC/event edges up to 3 hops:
- **Affected files** — everything that imports or calls the changed code
- **Affected services** — which microservices are in the blast radius
- **Affected contracts** — API contracts (proto/OpenAPI/GraphQL) that may break
- **Dead exports in changed files** — stale code that could be cleaned up alongside the PR

## tRPC API Contracts (Phase 2)
`graph_trpc_routes()` — extracts procedures from tRPC routers as typed API contract nodes.

Detects `t.router({})`, `t.procedure.query()`, `.mutation()`, `.subscription()` chains.
Procedure types: `query` | `mutation` | `subscription`.

## Message Queue Support
| System | Detect publish | Detect consume |
|--------|---------------|----------------|
| Kafka | ✓ | ✓ |
| SQS | ✓ | — |
| SNS | ✓ | — |
| Redis pub/sub | ✓ | ✓ |
| RabbitMQ / AMQP | ✓ | ✓ |
| NATS | ✓ | ✓ |
| AWS EventBridge | ✓ | — |

## PR Review & Security Gate (Phase 1)

### Diff-Aware Security Gate
`review.py` runs SAST + secrets + OSV only on added lines in PR diffs — zero noise from existing code.

| Scanner | What it checks | Trigger |
|---------|---------------|---------|
| SAST | All 14 SAST rules (see above) | Any `.py`/`.js`/`.ts`/`.go`/`.java` file in diff |
| Secrets | Hardcoded AWS keys, tokens, passwords | Any file in diff |
| OSV | Known CVEs in pinned dependencies | Manifest files changed in diff |

- `_structured_verdict()` → FAIL (critical/secret), WARN (high), PASS (clean)
- `_post_status_check()` → GitHub commit status API (pass/pending/failure)
- Findings injected into Claude review prompt for context-aware AI comments

### No-Code Codegen Pipeline
`graph_codegen_pr()` — generate code from natural language, validate, and open a PR.

| Stage | What it does |
|-------|-------------|
| Context | Reads target files, detects language, gathers graph context |
| Generate | Claude produces complete file contents from intent description |
| Security Gate | SAST + secrets scan on generated code — blocks on CRITICAL/HIGH |
| PR Creation | Commits, pushes branch, opens GitHub PR with gate results in body |

Gate FAIL → PR blocked with findings listed. Gate WARN → PR created with warning note.

## No-Code Developer Tools (codegen.py)

### Code Generation
| Tool | What it does |
|------|-------------|
| `graph_codegen_pr()` | Natural language → Claude generates code → security gate → GitHub PR |
| `graph_codegen_pr_autofix()` | Same but retries up to N times if gate fails |
| `graph_generate_tests()` | Generates pytest/jest/go_test/junit tests for existing code |
| `graph_refactor()` | Refactors code (clean/extract/rename/simplify/type_safety) with gate |
| `graph_api_stubs()` | API endpoint stubs from OpenAPI spec or description |
| `graph_generate_migration()` | DB migration from natural language (Django/SQLAlchemy/Prisma/SQL) |

### AI-Powered Writing
| Tool | What it does |
|------|-------------|
| `graph_explain_code()` | Plain-English explanation (developer/junior/non-technical) |
| `graph_commit_message()` | Conventional Commits / gitmoji / plain commit messages from diff |
| `graph_pr_description()` | PR title, body, labels, breaking_changes flag from git history |
| `graph_changelog()` | Keep-a-Changelog entry from git commits since a tag |
| `graph_generate_readme()` | README.md from codebase structure, manifests, env vars |

### Infrastructure
| Tool | What it does |
|------|-------------|
| `graph_generate_dockerfile()` | Optimized Dockerfile + .dockerignore. Multi-stage for Go/Rust/Java |
| `graph_generate_compose()` | docker-compose.yml with auto-detected databases |
| `graph_generate_ci()` | GitHub Actions (ci/cd/full/security/release) |
| `graph_extract_openapi()` | OpenAPI 3.0 spec from route definitions (Python/TS/Go) |

### Analysis & Quality
| Tool | What it does |
|------|-------------|
| `graph_review_files()` | Local code review (SAST + AI quality) without needing a PR |
| `graph_complexity()` | Cyclomatic complexity per function, sorted by risk |
| `graph_detect_duplicates()` | Copy-paste detection across files (sliding-window hash) |
| `graph_check_deps()` | Import analysis — stdlib vs third-party, needs-install list |
| `graph_upgrade_advisor()` | Flags pinned deps with outdated major versions |
| `graph_document_env_vars()` | Scans all env var usage, generates .env.example |

### Tooling & Standards
| Tool | What it does |
|------|-------------|
| `graph_config_files()` | Generates mypy/ruff/eslint/prettier/editorconfig/.gitignore/pytest.ini |
| `graph_precommit_config()` | .pre-commit-config.yaml with ruff/gitleaks/eslint/mypy/commitizen |
| `graph_add_license_headers()` | SPDX license headers (MIT/Apache/GPL). Skips existing. Preserves shebang |
| `graph_type_stubs()` | Python .pyi stub files from existing modules |

### Auto-Fix Loop
`graph_codegen_pr_autofix()` — if security gate fails, re-prompts Claude with the findings
to produce safe code. Retries up to N times (default 2).

### Test Generation
`graph_generate_tests()` — generates comprehensive tests for existing code.

| Framework | Auto-detected from |
|-----------|-------------------|
| pytest | `.py` files |
| jest | `.ts`/`.js` files |
| go_test | `.go` files |
| junit | `.java` files |

### Refactoring
`graph_refactor()` — restructures code while preserving behavior.

| Type | What it does |
|------|-------------|
| `clean` | Remove dead code, fix naming, improve readability |
| `extract` | Pull repeated logic into helper functions |
| `rename` | Improve variable/function names |
| `simplify` | Flatten nesting, simplify booleans |
| `type_safety` | Add type annotations, replace Any |

### Code Explanation
`graph_explain_code()` — plain-English explanation at three levels: developer, junior, non-technical.

### Dependency Impact
`graph_check_deps()` — analyzes imports, separates stdlib vs third-party, flags packages needing install.

### Local Code Review
`graph_review_files()` — review files without a PR. Combines SAST gate + AI quality review.
Focus modes: `all`, `security`, `performance`, `readability`.

### Migration Generator
`graph_generate_migration()` — generates database migrations from natural language.
Auto-detects ORM: Django (manage.py), SQLAlchemy (alembic.ini), Prisma (.prisma), or raw SQL.

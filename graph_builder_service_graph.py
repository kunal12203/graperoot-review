"""
graph_builder_service_graph.py — Phase 3: Service Graph (MQ + Contract Parsers)

Detects message queue publish/subscribe patterns (Kafka, SQS, SNS, Redis,
RabbitMQ, NATS, EventBridge, BullMQ) and parses contract files
(proto/gRPC, OpenAPI/Swagger, GraphQL, AsyncAPI).

Edge format:
    {"from": str, "to": str, "rel": "publishes_to"|"subscribes_from"|"imports"|"calls"}

Symbol format (standard):
    {
        "id": f"{file_path}::{name}",
        "name": str,
        "symbol_type": "model"|"use_case"|"api_route"|"utility"|"hook",
        "line_start": int,   # 0-indexed
        "line_end": int,
        "body_hash": str,    # md5[:8]
        "confidence": "high"|"medium"|"low",
        "exported": bool,
        "keywords": list[str],
    }
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTRACT_EXTS: set[str] = {".proto", ".graphql", ".gql"}
SERVICE_GRAPH_EXTS: set[str] = {".py", ".ts", ".tsx", ".js", ".jsx", ".kt", ".java", ".go"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _body_hash(lines: list[str], start: int, end: int) -> str:
    """Return first 8 hex chars of MD5 of lines[start:end+1]."""
    chunk = "".join(lines[start : end + 1])
    return hashlib.md5(chunk.encode("utf-8", errors="replace")).hexdigest()[:8]


def _name_keywords(name: str) -> list[str]:
    """Split a camelCase / snake_case / kebab-case name into lowercase tokens."""
    # Insert boundaries before uppercase runs and digits
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    parts = re.split(r"[_\-/:\s]+", s)
    return [p.lower() for p in parts if len(p) > 1]


def _find_block_end_brace(lines: list[str], start: int, limit: int = 200) -> int:
    """
    Given that lines[start] contains (or begins) an opening '{', scan forward
    to find the matching closing '}' using a depth counter.
    Returns the 0-indexed line number of the closing brace, capped at start+limit.
    """
    depth = 0
    end = min(start + limit, len(lines) - 1)
    for i in range(start, end + 1):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth <= 0 and i > start:
            return i
    return end


# ---------------------------------------------------------------------------
# 3A: Message Queue Detection
# ---------------------------------------------------------------------------

# ---- broker-specific pattern tables ----------------------------------------
#
# Each entry: (compiled_regex, rel, broker, confidence)
# Capture group 1 must always be the raw topic / queue / channel / subject string.
# If the topic is clearly a literal string → "high"; variable/computed → "medium".
#

def _build_mq_patterns() -> list[tuple]:
    """Build and return the list of (pattern, rel, broker, confidence) tuples."""

    H = "high"
    M = "medium"
    P = "publishes_to"
    S = "subscribes_from"

    # Shared string fragment: matches a single-quoted, double-quoted, or backtick string
    _sq = r"'([^']+)'"   # single-quoted literal
    _dq = r'"([^"]+)"'   # double-quoted literal
    _bq = r'`([^`]+)`'   # backtick literal

    def lit(*groups: str) -> str:
        """Alternate quoted literal patterns."""
        return "(?:" + "|".join(groups) + ")"

    ANY_LIT = lit(_sq, _dq, _bq)  # any quoted literal (group 1 in each branch → use named groups)

    patterns: list[tuple] = []

    # ------------------------------------------------------------------ Kafka
    # Python: producer.send('topic', ...) — any var name ending in producer/prod/kp
    patterns.append((re.compile(
        r'\b(?:\w+_)?(?:producer|prod|kp)\s*\.\s*send\s*\(\s*' + ANY_LIT, re.I), P, "kafka", H))
    # Generic: KafkaProducer().send('topic') inline
    patterns.append((re.compile(
        r'KafkaProducer\s*\([^)]*\)\s*\.\s*send\s*\(\s*' + ANY_LIT, re.I), P, "kafka", H))

    # Python: KafkaProducer().send('topic') — same regex catches it
    # Python: KafkaConsumer('topic', ...) constructor
    patterns.append((re.compile(
        r'KafkaConsumer\s*\(\s*' + ANY_LIT, re.I), S, "kafka", H))
    # Python: @kafka_consumer('topic') decorator
    patterns.append((re.compile(
        r'@kafka_consumer\s*\(\s*' + ANY_LIT, re.I), S, "kafka", H))

    # Python: consumer.subscribe(['topic']) — list with one literal
    patterns.append((re.compile(
        r'(?:consumer|kafka_consumer)\s*\.\s*subscribe\s*\(\s*\[\s*' + ANY_LIT, re.I), S, "kafka", H))

    # TS/JS: producer.send({ topic: 'name', ...})
    patterns.append((re.compile(
        r'producer\s*\.\s*send\s*\(\s*\{[^}]*\btopic\s*:\s*' + ANY_LIT, re.I | re.DOTALL), P, "kafka", H))

    # TS/JS: consumer.subscribe({ topic: 'name' })
    patterns.append((re.compile(
        r'consumer\s*\.\s*subscribe\s*\(\s*\{[^}]*\btopic\s*:\s*' + ANY_LIT, re.I | re.DOTALL), S, "kafka", H))

    # Java/Kotlin: @KafkaListener(topics = {"topic"}) — annotations
    patterns.append((re.compile(
        r'@KafkaListener\s*\([^)]*\btopics\s*=\s*\{?\s*' + ANY_LIT, re.I | re.DOTALL), S, "kafka", H))

    # Java/Kotlin: kafkaTemplate.send("topic", ...)
    patterns.append((re.compile(
        r'kafkaTemplate\s*\.\s*send\s*\(\s*' + ANY_LIT, re.I), P, "kafka", H))

    # Java/Kotlin: producer.send(new ProducerRecord("topic", ...))
    patterns.append((re.compile(
        r'new\s+ProducerRecord\s*\(\s*' + ANY_LIT, re.I), P, "kafka", H))

    # Go: writer.WriteMessages(kafka.Message{Topic: "name"})
    patterns.append((re.compile(
        r'WriteMessages\s*\([^)]*\bTopic\s*:\s*' + ANY_LIT, re.I | re.DOTALL), P, "kafka", H))

    # Go: kafka.NewReader(kafka.ReaderConfig{Topic: "name"})
    patterns.append((re.compile(
        r'NewReader\s*\([^)]*\bTopic\s*:\s*' + ANY_LIT, re.I | re.DOTALL), S, "kafka", H))

    # ------------------------------------------------------------------ SQS
    # Python: sqs.send_message(QueueUrl='...')
    patterns.append((re.compile(
        r'(?:sqs|sqs_client)\s*\.\s*send_message\s*\([^)]*\bQueueUrl\s*=\s*' + ANY_LIT,
        re.I | re.DOTALL), P, "sqs", H))

    # TS/JS: sqs.sendMessage({ QueueUrl: '...' })
    patterns.append((re.compile(
        r'(?:sqs|sqsClient)\s*\.\s*sendMessage\s*\(\s*\{[^}]*\bQueueUrl\s*:\s*' + ANY_LIT,
        re.I | re.DOTALL), P, "sqs", H))

    # Java: sqsClient.sendMessage(...)  — topic is variable, medium confidence
    patterns.append((re.compile(
        r'sqsClient\s*\.\s*sendMessage\s*\(', re.I), P, "sqs", M))

    # ------------------------------------------------------------------ SNS
    # Python: sns.publish(TopicArn='arn:aws:sns:...:topic-name')
    patterns.append((re.compile(
        r'(?:sns|sns_client)\s*\.\s*publish\s*\([^)]*\bTopicArn\s*=\s*' + ANY_LIT,
        re.I | re.DOTALL), P, "sns", H))

    # TS/JS: sns.publish({ TopicArn: 'arn:...' })
    patterns.append((re.compile(
        r'(?:sns|snsClient)\s*\.\s*publish\s*\(\s*\{[^}]*\bTopicArn\s*:\s*' + ANY_LIT,
        re.I | re.DOTALL), P, "sns", H))

    # Java: snsClient.publish(...)
    patterns.append((re.compile(
        r'snsClient\s*\.\s*publish\s*\(', re.I), P, "sns", M))

    # ------------------------------------------------------------------ Redis pub/sub
    # Python: redis.publish('channel', ...) / pubsub.publish(...)
    patterns.append((re.compile(
        r'(?:redis|r|client|pubsub)\s*\.\s*publish\s*\(\s*' + ANY_LIT, re.I), P, "redis", H))

    # Python: redis.subscribe('channel') / pubsub.subscribe('channel')
    patterns.append((re.compile(
        r'(?:redis|r|client|pubsub)\s*\.\s*subscribe\s*\(\s*' + ANY_LIT, re.I), S, "redis", H))

    # TS/JS: client.publish('channel', ...) — guard against kafka consumer.subscribe overlap
    patterns.append((re.compile(
        r'\bclient\s*\.\s*publish\s*\(\s*' + ANY_LIT, re.I), P, "redis", H))

    # TS/JS: client.subscribe('channel', ...)
    patterns.append((re.compile(
        r'\bclient\s*\.\s*subscribe\s*\(\s*' + ANY_LIT, re.I), S, "redis", H))

    # ------------------------------------------------------------------ RabbitMQ
    # Python: channel.basic_publish(exchange='', routing_key='queue', ...)
    patterns.append((re.compile(
        r'channel\s*\.\s*basic_publish\s*\([^)]*\brouting_key\s*=\s*' + ANY_LIT,
        re.I | re.DOTALL), P, "rabbitmq", H))

    # Fallback: basic_publish with exchange= captures exchange name
    patterns.append((re.compile(
        r'channel\s*\.\s*basic_publish\s*\([^)]*\bexchange\s*=\s*' + ANY_LIT,
        re.I | re.DOTALL), P, "rabbitmq", H))

    # Python: channel.basic_consume(queue='name', ...)
    patterns.append((re.compile(
        r'channel\s*\.\s*basic_consume\s*\([^)]*\bqueue\s*=\s*' + ANY_LIT,
        re.I | re.DOTALL), S, "rabbitmq", H))

    # TS/JS: channel.publish('exchange', 'routing_key', ...)
    patterns.append((re.compile(
        r'channel\s*\.\s*publish\s*\(\s*' + ANY_LIT, re.I), P, "rabbitmq", H))

    # TS/JS: channel.consume('queue', ...)
    patterns.append((re.compile(
        r'channel\s*\.\s*consume\s*\(\s*' + ANY_LIT, re.I), S, "rabbitmq", H))

    # Java: channel.basicPublish("exchange", "routingKey", ...)
    patterns.append((re.compile(
        r'channel\s*\.\s*basicPublish\s*\(\s*' + ANY_LIT, re.I), P, "rabbitmq", H))

    # Java: channel.basicConsume("queue", ...)
    patterns.append((re.compile(
        r'channel\s*\.\s*basicConsume\s*\(\s*' + ANY_LIT, re.I), S, "rabbitmq", H))

    # ------------------------------------------------------------------ NATS
    # Python / TS/JS: nc.publish('subject', ...)
    patterns.append((re.compile(
        r'\bnc\s*\.\s*publish\s*\(\s*' + ANY_LIT, re.I), P, "nats", H))

    # Python / TS/JS: nc.subscribe('subject', ...)
    patterns.append((re.compile(
        r'\bnc\s*\.\s*subscribe\s*\(\s*' + ANY_LIT, re.I), S, "nats", H))

    # Go: nc.Publish("subject", ...)
    patterns.append((re.compile(
        r'\bnc\s*\.\s*Publish\s*\(\s*' + ANY_LIT), P, "nats", H))

    # Go: nc.Subscribe("subject", ...)
    patterns.append((re.compile(
        r'\bnc\s*\.\s*Subscribe\s*\(\s*' + ANY_LIT), S, "nats", H))

    # ------------------------------------------------------------------ EventBridge
    # Python / TS/JS: events.put_events(Entries=[{...Source: 'foo', DetailType: 'bar'}])
    # We capture DetailType as the logical "topic"
    patterns.append((re.compile(
        r'(?:events|eventbridge)\s*\.\s*put_events\s*\([^)]*\bDetailType\s*[=:]\s*' + ANY_LIT,
        re.I | re.DOTALL), P, "eventbridge", H))

    # Fallback: capture Source
    patterns.append((re.compile(
        r'(?:events|eventbridge)\s*\.\s*put_events\s*\([^)]*\bSource\s*[=:]\s*' + ANY_LIT,
        re.I | re.DOTALL), P, "eventbridge", H))

    # ------------------------------------------------------------------ BullMQ / Bull (Node.js)
    # new Queue('queue-name')
    patterns.append((re.compile(
        r'new\s+Queue\s*\(\s*' + ANY_LIT, re.I), P, "bullmq", H))

    # queue.add('job-name', ...)  — this is a publish
    patterns.append((re.compile(
        r'(?:queue|bull)\s*\.\s*add\s*\(\s*' + ANY_LIT, re.I), P, "bullmq", H))

    # new Worker('queue-name', ...) — BullMQ worker subscribes
    patterns.append((re.compile(
        r'new\s+Worker\s*\(\s*' + ANY_LIT, re.I), S, "bullmq", H))

    # worker.on('queue-name', ...) — older Bull pattern
    patterns.append((re.compile(
        r'worker\s*\.\s*on\s*\(\s*' + ANY_LIT, re.I), S, "bullmq", H))

    # queue.process('job-name', ...) — Bull subscriber
    patterns.append((re.compile(
        r'queue\s*\.\s*process\s*\(\s*' + ANY_LIT, re.I), S, "bullmq", H))

    return patterns


_MQ_PATTERNS: list[tuple] = _build_mq_patterns()


def _extract_topic_from_match(m: re.Match) -> Optional[str]:
    """Return the first non-None group from a regex match (the captured topic)."""
    for g in m.groups():
        if g is not None:
            return g
    return None


def _topic_id(broker: str, topic: str) -> str:
    """Format a canonical topic identifier."""
    return f"{broker}:{topic}"


def extract_mq_edges(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Detect publish/subscribe patterns and return edges with
    rel='publishes_to' or 'subscribes_from'.

    Handles Python, TypeScript/JavaScript, Java/Kotlin, Go.
    """
    if ext not in SERVICE_GRAPH_EXTS:
        return []

    lines = content.splitlines()
    edges: list[dict] = []
    seen: set[tuple] = set()  # (line, broker, rel, topic) dedup

    for pattern, rel, broker, confidence in _MQ_PATTERNS:
        for m in pattern.finditer(content):
            topic = _extract_topic_from_match(m)
            if topic is None:
                # Medium-confidence match with no literal topic — synthesise a placeholder
                topic = f"<variable:{broker}>"
                confidence = "medium"

            # Compute line number (0-indexed)
            line_no = content[: m.start()].count("\n")

            key = (line_no, broker, rel, topic)
            if key in seen:
                continue
            seen.add(key)

            topic_id = _topic_id(broker, topic)
            edges.append({
                "from": file_path,
                "to": topic_id,
                "rel": rel,
                "broker": broker,
                "topic": topic,
                "line": line_no,
                "confidence": confidence,
            })

    return edges


# ---- annotation-based MQ symbol extraction --------------------------------

# Patterns that mark class/method-level listener declarations
_ANNOTATION_PATTERNS: list[tuple] = [
    # Java/Spring: @KafkaListener(topics = {"topic"})
    (re.compile(
        r'@KafkaListener\s*\([^)]*\btopics\s*=\s*\{?\s*(?:\'([^\']+)\'|"([^"]+)")',
        re.I | re.DOTALL),
     "kafka", "subscribes_from"),
    # Java/Spring: @RabbitListener(queues = {"queue"})
    (re.compile(
        r'@RabbitListener\s*\([^)]*\bqueues\s*=\s*\{?\s*(?:\'([^\']+)\'|"([^"]+)")',
        re.I | re.DOTALL),
     "rabbitmq", "subscribes_from"),
    # AWS SQS listener (Spring Cloud AWS)
    (re.compile(
        r'@SqsListener\s*\(\s*(?:value\s*=\s*)?(?:\'([^\']+)\'|"([^"]+)")',
        re.I | re.DOTALL),
     "sqs", "subscribes_from"),
    # NATS subject annotations (custom)
    (re.compile(
        r'@NatsSubscription\s*\(\s*(?:subject\s*=\s*)?(?:\'([^\']+)\'|"([^"]+)")',
        re.I | re.DOTALL),
     "nats", "subscribes_from"),
    # Python: @kafka_consumer('topic')
    (re.compile(
        r'@kafka_consumer\s*\(\s*(?:\'([^\']+)\'|"([^"]+)")',
        re.I),
     "kafka", "subscribes_from"),
]

# Next-line pattern to capture the annotated class/function name
_CLASS_OR_FUNC_RE = re.compile(
    r'(?:(?:public|private|protected|static|async|def|fun|func)\s+)*'
    r'(?:class|interface|object|fun|def|function|async\s+function)?\s*'
    r'([A-Za-z_][A-Za-z0-9_]*)',
    re.I,
)


def extract_mq_symbols(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract @KafkaListener, @RabbitListener, @SqsListener (and similar)
    class/method annotations as symbols.
    """
    lines = content.splitlines()
    symbols: list[dict] = []

    for ann_pattern, broker, rel in _ANNOTATION_PATTERNS:
        for m in ann_pattern.finditer(content):
            topic = _extract_topic_from_match(m)
            if topic is None:
                topic = f"<variable:{broker}>"

            line_no = content[: m.start()].count("\n")

            # Try to grab the name of the class/method on the NEXT non-blank line(s)
            name: str = f"{broker}_listener"
            for look_ahead in range(line_no + 1, min(line_no + 4, len(lines))):
                stripped = lines[look_ahead].strip()
                if not stripped or stripped.startswith("@"):
                    continue
                nm = _CLASS_OR_FUNC_RE.match(stripped)
                if nm:
                    name = nm.group(1)
                break

            line_end = _find_block_end_brace(lines, line_no)

            sym_name = f"{broker}:{name}:{topic}"
            symbols.append({
                "id": f"{file_path}::{sym_name}",
                "name": sym_name,
                "symbol_type": "hook",
                "line_start": line_no,
                "line_end": line_end,
                "body_hash": _body_hash(lines, line_no, line_end),
                "confidence": "high",
                "exported": True,
                "keywords": [broker, rel.replace("_", " ")] + _name_keywords(name) + _name_keywords(topic),
            })

    return symbols


# ---------------------------------------------------------------------------
# 3B: Contract File Parsers
# ---------------------------------------------------------------------------

# ---- Proto / gRPC -----------------------------------------------------------

_PROTO_SERVICE_RE = re.compile(r'^\s*service\s+(\w+)\s*\{', re.M)
_PROTO_RPC_RE = re.compile(
    r'^\s*rpc\s+(\w+)\s*\(\s*(\w+)\s*\)\s*returns\s*\(\s*(\w+)\s*\)', re.M)
_PROTO_MESSAGE_RE = re.compile(r'^\s*message\s+(\w+)\s*\{', re.M)
_PROTO_ENUM_RE = re.compile(r'^\s*enum\s+(\w+)\s*\{', re.M)
_PROTO_IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"', re.M)


def extract_symbols_proto(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    """
    Extract service, rpc, message, enum definitions from .proto files.
    Also returns import edges.
    Returns (symbols, edges).
    """
    lines = content.splitlines()
    symbols: list[dict] = []
    edges: list[dict] = []

    # Services
    for m in _PROTO_SERVICE_RE.finditer(content):
        svc_name = m.group(1)
        line_no = content[: m.start()].count("\n")
        line_end = _find_block_end_brace(lines, line_no)
        sym_name = f"service:{svc_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "api_route",
            "line_start": line_no,
            "line_end": line_end,
            "body_hash": _body_hash(lines, line_no, line_end),
            "confidence": "high",
            "exported": True,
            "keywords": ["service", "grpc"] + _name_keywords(svc_name),
        })

    # RPCs
    for m in _PROTO_RPC_RE.finditer(content):
        rpc_name = m.group(1)
        req_type = m.group(2)
        resp_type = m.group(3)
        line_no = content[: m.start()].count("\n")
        sym_name = f"rpc:{rpc_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "api_route",
            "line_start": line_no,
            "line_end": line_no,
            "body_hash": _body_hash(lines, line_no, line_no),
            "confidence": "high",
            "exported": True,
            "keywords": ["rpc", "grpc"] + _name_keywords(rpc_name) + _name_keywords(req_type) + _name_keywords(resp_type),
        })

    # Messages
    for m in _PROTO_MESSAGE_RE.finditer(content):
        msg_name = m.group(1)
        line_no = content[: m.start()].count("\n")
        line_end = _find_block_end_brace(lines, line_no)
        sym_name = f"message:{msg_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "model",
            "line_start": line_no,
            "line_end": line_end,
            "body_hash": _body_hash(lines, line_no, line_end),
            "confidence": "high",
            "exported": True,
            "keywords": ["message", "proto"] + _name_keywords(msg_name),
        })

    # Enums
    for m in _PROTO_ENUM_RE.finditer(content):
        enum_name = m.group(1)
        line_no = content[: m.start()].count("\n")
        line_end = _find_block_end_brace(lines, line_no)
        sym_name = f"enum:{enum_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "model",
            "line_start": line_no,
            "line_end": line_end,
            "body_hash": _body_hash(lines, line_no, line_end),
            "confidence": "high",
            "exported": True,
            "keywords": ["enum", "proto"] + _name_keywords(enum_name),
        })

    # Import edges
    for m in _PROTO_IMPORT_RE.finditer(content):
        imported = m.group(1)
        edges.append({
            "from": file_path,
            "to": imported,
            "rel": "imports",
        })

    return symbols, edges


# ---- OpenAPI / Swagger -------------------------------------------------------

_OPENAPI_KEY_RE = re.compile(r'^\s*(?:openapi|swagger)\s*:', re.M)
_OPENAPI_PATH_RE = re.compile(r'^\s{0,4}(/[^\s:]+)\s*:', re.M)
_OPENAPI_METHOD_RE = re.compile(r'^\s{4,6}(get|post|put|patch|delete|head|options|trace)\s*:', re.M | re.I)
_OPENAPI_PATH_METHOD_RE = re.compile(
    r'^(\s{0,4})(/[^\s:]+)\s*:\s*\n(?:.*\n)*?(?=\1\s{4,6}(get|post|put|patch|delete|head|options|trace)\s*:)',
    re.M | re.I,
)
_OPENAPI_SCHEMA_RE = re.compile(r'^\s{4,8}(\w[\w./-]*)\s*:\s*$', re.M)
_OPENAPI_TITLE_RE = re.compile(r'^\s*title\s*:\s*(.+)$', re.M)
_OPENAPI_VERSION_RE = re.compile(r'^\s*version\s*:\s*(.+)$', re.M)
_OPENAPI_COMPONENTS_SCHEMAS_RE = re.compile(
    r'components\s*:.*?schemas\s*:\s*\n((?:[ \t]+\S[^\n]*\n)*)',
    re.S,
)
_OPENAPI_SCHEMA_NAME_RE = re.compile(r'^[ \t]+(\w[\w./-]*)\s*:', re.M)

# Detect a paths block and enumerate methods per path
_OPENAPI_PATHS_BLOCK_RE = re.compile(r'^paths\s*:\s*\n(.*?)(?=^\S|\Z)', re.M | re.S)
_OPENAPI_PATH_ENTRY_RE = re.compile(r'^( {0,4})(/[^\n]+):\s*$', re.M)
_OPENAPI_METHOD_ENTRY_RE = re.compile(
    r'^( {4,8})(get|post|put|patch|delete|head|options|trace)\s*:', re.M | re.I)


def _is_openapi(content: str) -> bool:
    return bool(_OPENAPI_KEY_RE.search(content))


def extract_symbols_openapi(content: str, file_path: str) -> list[dict]:
    """
    Extract endpoints and schemas from OpenAPI/Swagger specs.
    Regex-only; handles YAML and JSON superficially.
    """
    lines = content.splitlines()
    symbols: list[dict] = []

    # --- Collect title/version for keyword enrichment ---
    title_kw: list[str] = []
    tm = _OPENAPI_TITLE_RE.search(content)
    if tm:
        title_kw = _name_keywords(tm.group(1).strip())
    vm = _OPENAPI_VERSION_RE.search(content)
    version_kw: list[str] = []
    if vm:
        version_kw = [vm.group(1).strip()]

    # --- Parse paths block: find each /path: then methods below it ---
    # Strategy: scan line by line looking for path entries then methods until
    # the next path-level entry (same or lower indent).
    current_path: Optional[str] = None
    path_indent: int = 0
    path_line: int = 0

    for i, line in enumerate(lines):
        # Detect a path entry (line starting with optional indent then / )
        pm = re.match(r'^( *)(\/[^\s:]*)\s*:\s*$', line)
        if pm:
            current_path = pm.group(2)
            path_indent = len(pm.group(1))
            path_line = i
            continue

        if current_path is None:
            continue

        # Detect an HTTP method entry under the current path
        mm = re.match(r'^( +)(get|post|put|patch|delete|head|options|trace)\s*:',
                      line, re.I)
        if mm:
            method_indent = len(mm.group(1))
            # Only accept if this method is indented exactly more than path_indent
            if method_indent > path_indent:
                method = mm.group(2).upper()
                route_name = f"{method} {current_path}"
                sym_name = route_name
                symbols.append({
                    "id": f"{file_path}::{sym_name}",
                    "name": sym_name,
                    "symbol_type": "api_route",
                    "line_start": i,
                    "line_end": i,
                    "body_hash": _body_hash(lines, i, i),
                    "confidence": "high",
                    "exported": True,
                    "keywords": title_kw + version_kw + [method.lower()] + _name_keywords(current_path),
                })

    # --- Parse components/schemas ---
    cs_match = _OPENAPI_COMPONENTS_SCHEMAS_RE.search(content)
    if cs_match:
        schemas_block = cs_match.group(1)
        schema_start = content.index(cs_match.group(1)) if cs_match.group(1) in content else 0
        for sm in _OPENAPI_SCHEMA_NAME_RE.finditer(schemas_block):
            schema_name = sm.group(1)
            abs_pos = schema_start + sm.start()
            line_no = content[:abs_pos].count("\n")
            sym_name = f"schema:{schema_name}"
            symbols.append({
                "id": f"{file_path}::{sym_name}",
                "name": sym_name,
                "symbol_type": "model",
                "line_start": line_no,
                "line_end": line_no,
                "body_hash": _body_hash(lines, line_no, line_no),
                "confidence": "high",
                "exported": True,
                "keywords": title_kw + ["schema", "openapi"] + _name_keywords(schema_name),
            })

    # --- JSON OpenAPI: definitions (Swagger 2.0) ---
    _def_re = re.compile(r'"definitions"\s*:\s*\{', re.S)
    dm = _def_re.search(content)
    if dm:
        # Grab definition names
        for snm in re.finditer(r'"(\w+)"\s*:\s*\{', content[dm.end():]):
            line_no = content[: dm.end() + snm.start()].count("\n")
            schema_name = snm.group(1)
            sym_name = f"schema:{schema_name}"
            # Avoid duplicates
            if not any(s["name"] == sym_name for s in symbols):
                symbols.append({
                    "id": f"{file_path}::{sym_name}",
                    "name": sym_name,
                    "symbol_type": "model",
                    "line_start": line_no,
                    "line_end": line_no,
                    "body_hash": _body_hash(lines, line_no, line_no),
                    "confidence": "high",
                    "exported": True,
                    "keywords": title_kw + ["schema", "swagger"] + _name_keywords(schema_name),
                })

    return symbols


# ---- GraphQL -----------------------------------------------------------------

_GQL_TYPE_RE = re.compile(r'^\s*type\s+(\w+)(?:\s+implements\s+\w+)?\s*\{', re.M)
_GQL_INPUT_RE = re.compile(r'^\s*input\s+(\w+)\s*\{', re.M)
_GQL_ENUM_RE = re.compile(r'^\s*enum\s+(\w+)\s*\{', re.M)
_GQL_INTERFACE_RE = re.compile(r'^\s*interface\s+(\w+)\s*\{', re.M)
_GQL_SCALAR_RE = re.compile(r'^\s*scalar\s+(\w+)', re.M)
_GQL_UNION_RE = re.compile(r'^\s*union\s+(\w+)', re.M)
_GQL_DIRECTIVE_RE = re.compile(r'^\s*directive\s+@(\w+)', re.M)
_GQL_FIELD_RE = re.compile(r'^\s{2,6}(\w+)\s*(?:\([^)]*\))?\s*:\s*[\w\[\]!]+', re.M)
_GQL_FRAGMENT_RE = re.compile(r'^\s*fragment\s+(\w+)\s+on\s+(\w+)', re.M)
_GQL_OPERATION_RE = re.compile(
    r'^\s*(?:query|mutation|subscription)\s+(\w+)', re.M)


def extract_symbols_graphql(content: str, file_path: str) -> list[dict]:
    """
    Extract types, queries, mutations, subscriptions from a GraphQL schema or document.
    """
    lines = content.splitlines()
    symbols: list[dict] = []

    # Identify special root types
    query_type_name: str = "Query"
    mutation_type_name: str = "Mutation"
    subscription_type_name: str = "Subscription"

    # Override from schema definition if present
    _schema_block_re = re.compile(r'schema\s*\{([^}]+)\}', re.S)
    sb = _schema_block_re.search(content)
    if sb:
        q_m = re.search(r'query\s*:\s*(\w+)', sb.group(1))
        if q_m:
            query_type_name = q_m.group(1)
        mu_m = re.search(r'mutation\s*:\s*(\w+)', sb.group(1))
        if mu_m:
            mutation_type_name = mu_m.group(1)
        su_m = re.search(r'subscription\s*:\s*(\w+)', sb.group(1))
        if su_m:
            subscription_type_name = su_m.group(1)

    # Named operations (query/mutation/subscription blocks in documents)
    for m in _GQL_OPERATION_RE.finditer(content):
        op_name = m.group(1)
        line_no = content[: m.start()].count("\n")
        raw_line = lines[line_no] if line_no < len(lines) else ""
        if "subscription" in raw_line.lower():
            sym_type = "hook"
        elif "mutation" in raw_line.lower():
            sym_type = "api_route"
        else:
            sym_type = "api_route"
        line_end = _find_block_end_brace(lines, line_no)
        sym_name = f"operation:{op_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": line_end,
            "body_hash": _body_hash(lines, line_no, line_end),
            "confidence": "high",
            "exported": True,
            "keywords": ["graphql", sym_type] + _name_keywords(op_name),
        })

    # Type definitions
    for m in _GQL_TYPE_RE.finditer(content):
        type_name = m.group(1)
        if type_name in ("schema",):
            continue
        line_no = content[: m.start()].count("\n")
        line_end = _find_block_end_brace(lines, line_no)

        if type_name == query_type_name:
            # Extract each field of Query as its own api_route symbol
            _emit_graphql_root_fields(content, lines, file_path, m.start(), line_end,
                                       "api_route", "query", symbols)
        elif type_name == mutation_type_name:
            _emit_graphql_root_fields(content, lines, file_path, m.start(), line_end,
                                       "api_route", "mutation", symbols)
        elif type_name == subscription_type_name:
            _emit_graphql_root_fields(content, lines, file_path, m.start(), line_end,
                                       "hook", "subscription", symbols)
        else:
            sym_name = f"type:{type_name}"
            symbols.append({
                "id": f"{file_path}::{sym_name}",
                "name": sym_name,
                "symbol_type": "model",
                "line_start": line_no,
                "line_end": line_end,
                "body_hash": _body_hash(lines, line_no, line_end),
                "confidence": "high",
                "exported": True,
                "keywords": ["type", "graphql"] + _name_keywords(type_name),
            })

    # Input types
    for m in _GQL_INPUT_RE.finditer(content):
        input_name = m.group(1)
        line_no = content[: m.start()].count("\n")
        line_end = _find_block_end_brace(lines, line_no)
        sym_name = f"input:{input_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "model",
            "line_start": line_no,
            "line_end": line_end,
            "body_hash": _body_hash(lines, line_no, line_end),
            "confidence": "high",
            "exported": True,
            "keywords": ["input", "graphql"] + _name_keywords(input_name),
        })

    # Enums
    for m in _GQL_ENUM_RE.finditer(content):
        enum_name = m.group(1)
        line_no = content[: m.start()].count("\n")
        line_end = _find_block_end_brace(lines, line_no)
        sym_name = f"enum:{enum_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "model",
            "line_start": line_no,
            "line_end": line_end,
            "body_hash": _body_hash(lines, line_no, line_end),
            "confidence": "high",
            "exported": True,
            "keywords": ["enum", "graphql"] + _name_keywords(enum_name),
        })

    # Interfaces
    for m in _GQL_INTERFACE_RE.finditer(content):
        iface_name = m.group(1)
        line_no = content[: m.start()].count("\n")
        line_end = _find_block_end_brace(lines, line_no)
        sym_name = f"interface:{iface_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "utility",
            "line_start": line_no,
            "line_end": line_end,
            "body_hash": _body_hash(lines, line_no, line_end),
            "confidence": "high",
            "exported": True,
            "keywords": ["interface", "graphql"] + _name_keywords(iface_name),
        })

    # Scalars
    for m in _GQL_SCALAR_RE.finditer(content):
        scalar_name = m.group(1)
        line_no = content[: m.start()].count("\n")
        sym_name = f"scalar:{scalar_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "utility",
            "line_start": line_no,
            "line_end": line_no,
            "body_hash": _body_hash(lines, line_no, line_no),
            "confidence": "high",
            "exported": True,
            "keywords": ["scalar", "graphql"] + _name_keywords(scalar_name),
        })

    # Unions
    for m in _GQL_UNION_RE.finditer(content):
        union_name = m.group(1)
        line_no = content[: m.start()].count("\n")
        sym_name = f"union:{union_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "model",
            "line_start": line_no,
            "line_end": line_no,
            "body_hash": _body_hash(lines, line_no, line_no),
            "confidence": "high",
            "exported": True,
            "keywords": ["union", "graphql"] + _name_keywords(union_name),
        })

    # Fragments (document-level)
    for m in _GQL_FRAGMENT_RE.finditer(content):
        frag_name = m.group(1)
        on_type = m.group(2)
        line_no = content[: m.start()].count("\n")
        line_end = _find_block_end_brace(lines, line_no)
        sym_name = f"fragment:{frag_name}"
        symbols.append({
            "id": f"{file_path}::{sym_name}",
            "name": sym_name,
            "symbol_type": "utility",
            "line_start": line_no,
            "line_end": line_end,
            "body_hash": _body_hash(lines, line_no, line_end),
            "confidence": "high",
            "exported": True,
            "keywords": ["fragment", "graphql"] + _name_keywords(frag_name) + _name_keywords(on_type),
        })

    return symbols


def _emit_graphql_root_fields(
    content: str,
    lines: list[str],
    file_path: str,
    type_start_pos: int,
    type_line_end: int,
    sym_type: str,
    operation_kind: str,
    symbols: list[dict],
) -> None:
    """
    For a root GraphQL type (Query/Mutation/Subscription), emit each field
    as an individual symbol.
    """
    type_line_start = content[:type_start_pos].count("\n")
    # Extract the lines of the type body
    for i in range(type_line_start + 1, min(type_line_end, len(lines))):
        field_m = re.match(r'\s{1,8}(\w+)\s*(?:\([^)]*\))?\s*:\s*[\w\[\]!]', lines[i])
        if field_m:
            field_name = field_m.group(1)
            sym_name = f"{operation_kind}:{field_name}"
            symbols.append({
                "id": f"{file_path}::{sym_name}",
                "name": sym_name,
                "symbol_type": sym_type,
                "line_start": i,
                "line_end": i,
                "body_hash": _body_hash(lines, i, i),
                "confidence": "high",
                "exported": True,
                "keywords": [operation_kind, "graphql"] + _name_keywords(field_name),
            })


# ---- AsyncAPI ----------------------------------------------------------------

_ASYNCAPI_KEY_RE = re.compile(r'^\s*asyncapi\s*:', re.M)
_ASYNCAPI_CHANNELS_BLOCK_RE = re.compile(
    r'^channels\s*:\s*\n(.*?)(?=^\S|\Z)', re.M | re.S)
_ASYNCAPI_CHANNEL_ENTRY_RE = re.compile(r'^( {0,4})([^\s:][^\n]*)\s*:\s*$', re.M)
_ASYNCAPI_OPERATION_RE = re.compile(r'^( {4,8})(subscribe|publish)\s*:', re.M | re.I)
_ASYNCAPI_COMP_MSG_RE = re.compile(
    r'components\s*:.*?messages\s*:\s*\n((?:[ \t]+\S[^\n]*\n)*)', re.S)
_ASYNCAPI_COMP_SCH_RE = re.compile(
    r'components\s*:.*?schemas\s*:\s*\n((?:[ \t]+\S[^\n]*\n)*)', re.S)
_ASYNCAPI_ITEM_NAME_RE = re.compile(r'^[ \t]+(\w[\w./-]*)\s*:', re.M)
_ASYNCAPI_INFO_TITLE_RE = re.compile(r'^\s*title\s*:\s*(.+)$', re.M)


def _is_asyncapi(content: str) -> bool:
    return bool(_ASYNCAPI_KEY_RE.search(content))


def extract_symbols_asyncapi(content: str, file_path: str) -> list[dict]:
    """
    Extract channels, messages, operations, and schemas from AsyncAPI specs.
    """
    lines = content.splitlines()
    symbols: list[dict] = []

    title_kw: list[str] = []
    tm = _ASYNCAPI_INFO_TITLE_RE.search(content)
    if tm:
        title_kw = _name_keywords(tm.group(1).strip())

    # --- Channels block ---
    ch_block_m = _ASYNCAPI_CHANNELS_BLOCK_RE.search(content)
    if ch_block_m:
        ch_block = ch_block_m.group(1)
        ch_block_start = content.index(ch_block_m.group(1)) if ch_block_m.group(1) in content else 0

        current_channel: Optional[str] = None
        current_channel_indent: int = 0
        current_channel_abs_line: int = 0

        # Scan lines of the channels block
        ch_block_lines = ch_block.splitlines()
        ch_block_line_offset = content[:ch_block_start].count("\n")

        for j, bl in enumerate(ch_block_lines):
            abs_line = ch_block_line_offset + j

            # Channel entry: no leading spaces (relative to block) or up to 4
            ce_m = re.match(r'^( {0,4})([^\s:][^\n:]+?)\s*:\s*$', bl)
            if ce_m:
                current_channel = ce_m.group(2).strip()
                current_channel_indent = len(ce_m.group(1))
                current_channel_abs_line = abs_line
                continue

            if current_channel is None:
                continue

            # subscribe / publish operation
            op_m = re.match(r'^( +)(subscribe|publish)\s*:', bl, re.I)
            if op_m:
                op_indent = len(op_m.group(1))
                op_kind = op_m.group(2).lower()
                if op_indent > current_channel_indent:
                    sym_name = f"channel:{current_channel}:{op_kind}"
                    symbols.append({
                        "id": f"{file_path}::{sym_name}",
                        "name": sym_name,
                        "symbol_type": "hook",
                        "line_start": abs_line,
                        "line_end": abs_line,
                        "body_hash": _body_hash(lines, abs_line, abs_line),
                        "confidence": "high",
                        "exported": True,
                        "keywords": title_kw + ["asyncapi", op_kind, "channel"] + _name_keywords(current_channel),
                    })

    # --- components/messages ---
    msg_block_m = _ASYNCAPI_COMP_MSG_RE.search(content)
    if msg_block_m:
        msg_block = msg_block_m.group(1)
        msg_start = content.index(msg_block) if msg_block in content else 0
        for nm in _ASYNCAPI_ITEM_NAME_RE.finditer(msg_block):
            msg_name = nm.group(1)
            abs_pos = msg_start + nm.start()
            line_no = content[:abs_pos].count("\n")
            sym_name = f"message:{msg_name}"
            symbols.append({
                "id": f"{file_path}::{sym_name}",
                "name": sym_name,
                "symbol_type": "model",
                "line_start": line_no,
                "line_end": line_no,
                "body_hash": _body_hash(lines, line_no, line_no),
                "confidence": "high",
                "exported": True,
                "keywords": title_kw + ["asyncapi", "message"] + _name_keywords(msg_name),
            })

    # --- components/schemas ---
    sch_block_m = _ASYNCAPI_COMP_SCH_RE.search(content)
    if sch_block_m:
        sch_block = sch_block_m.group(1)
        sch_start = content.index(sch_block) if sch_block in content else 0
        for nm in _ASYNCAPI_ITEM_NAME_RE.finditer(sch_block):
            schema_name = nm.group(1)
            abs_pos = sch_start + nm.start()
            line_no = content[:abs_pos].count("\n")
            sym_name = f"schema:{schema_name}"
            symbols.append({
                "id": f"{file_path}::{sym_name}",
                "name": sym_name,
                "symbol_type": "model",
                "line_start": line_no,
                "line_end": line_no,
                "body_hash": _body_hash(lines, line_no, line_no),
                "confidence": "high",
                "exported": True,
                "keywords": title_kw + ["asyncapi", "schema"] + _name_keywords(schema_name),
            })

    return symbols


# ---------------------------------------------------------------------------
# Main dispatcher functions
# ---------------------------------------------------------------------------

def is_contract_file(file_path: str, content_hint: str = "") -> bool:
    """
    Returns True if this file is a contract file (proto, GraphQL, OpenAPI, AsyncAPI).
    Uses file extension first; falls back to content sniffing for YAML/JSON.
    """
    import os
    _, ext = os.path.splitext(file_path.lower())

    if ext in CONTRACT_EXTS:
        return True

    if ext in (".yaml", ".yml", ".json") and content_hint:
        if _OPENAPI_KEY_RE.search(content_hint):
            return True
        if _ASYNCAPI_KEY_RE.search(content_hint):
            return True

    return False


def extract_contract_symbols(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    """
    Detect and extract symbols (and edges) from contract files.
    Dispatches to proto/OpenAPI/GraphQL/AsyncAPI parsers.

    Returns (symbols, edges).
    """
    import os
    _, ext = os.path.splitext(file_path.lower())

    symbols: list[dict] = []
    edges: list[dict] = []

    if ext == ".proto":
        syms, edgs = extract_symbols_proto(content, file_path)
        symbols.extend(syms)
        edges.extend(edgs)

    elif ext in (".graphql", ".gql"):
        symbols.extend(extract_symbols_graphql(content, file_path))

    elif ext in (".yaml", ".yml"):
        if _is_asyncapi(content):
            symbols.extend(extract_symbols_asyncapi(content, file_path))
        elif _is_openapi(content):
            symbols.extend(extract_symbols_openapi(content, file_path))

    elif ext == ".json":
        if _is_openapi(content):
            symbols.extend(extract_symbols_openapi(content, file_path))

    return symbols, edges


def extract_service_graph_edges(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract all MQ edges from a source file.
    Combines edges from extract_mq_edges.
    """
    return extract_mq_edges(content, file_path, ext)


# ---------------------------------------------------------------------------
# Convenience: extract both symbols and edges for any file
# ---------------------------------------------------------------------------

def extract_all(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    """
    High-level entry point: given file content and path, return
    (symbols, edges) covering both contract parsing and MQ detection.

    Callers that iterate over a project's files can call this once per file.
    """
    import os
    _, ext = os.path.splitext(file_path.lower())

    all_symbols: list[dict] = []
    all_edges: list[dict] = []

    # Contract files
    if is_contract_file(file_path, content):
        syms, edgs = extract_contract_symbols(content, file_path)
        all_symbols.extend(syms)
        all_edges.extend(edgs)

    # Source files (MQ edges + annotation symbols)
    if ext in SERVICE_GRAPH_EXTS:
        all_edges.extend(extract_service_graph_edges(content, file_path, ext))
        all_symbols.extend(extract_mq_symbols(content, file_path, ext))

    return all_symbols, all_edges

"""
IaC Extended symbol extractor for GrapeRoot Pro graph_builder_v6.2+.

Static-analysis (regex-only, no execution, no AST) symbol extraction for:
  1. AWS CDK     (TypeScript/JavaScript/Python)
  2. AWS CloudFormation  (YAML + JSON)
  3. Pulumi      (TypeScript/JavaScript/Python)
  4. Azure Bicep (.bicep)
  5. Azure ARM JSON

Public API
----------
IAC_EXT_EXTS    : set[str]   — all file extensions handled
IAC_CDK_EXTS    : set[str]   — CDK-specific extensions
IAC_CFN_EXTS    : set[str]   — CloudFormation template extensions
IAC_PULUMI_EXTS : set[str]   — Pulumi program extensions
IAC_BICEP_EXTS  : set[str]   — Bicep + ARM JSON extensions

supports_iac_extended(content, file_path, ext) -> bool
extract_iac_extended_symbols(content, file_path, ext) -> list[dict]
parse_iac_extended_imports(content, file_id, ext) -> list[dict]
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# File extension constants
# ---------------------------------------------------------------------------

IAC_CDK_EXTS: set[str] = {".ts", ".js", ".py", ".java", ".go"}
IAC_CFN_EXTS: set[str] = {".yml", ".yaml", ".json", ".template"}
IAC_PULUMI_EXTS: set[str] = {".ts", ".js", ".py", ".go"}
IAC_BICEP_EXTS: set[str] = {".bicep", ".json"}

IAC_EXT_EXTS: set[str] = IAC_CDK_EXTS | IAC_CFN_EXTS | IAC_BICEP_EXTS

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _body_hash(lines: list[str], start: int, end: int) -> str:
    """SHA-256[:12] of the lines in [start, end] (0-indexed, inclusive)."""
    body = "\n".join(lines[start : end + 1])
    return hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _name_keywords(name: str, extras: Optional[list[str]] = None) -> list[str]:
    """Split CamelCase / snake_case / kebab-case identifier into keyword tokens."""
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(w: str) -> None:
        w = re.sub(r"[^a-z0-9]", "", w.lower())
        if len(w) >= 2 and w not in seen:
            seen.add(w)
            tokens.append(w)

    _add(name)
    # CamelCase split
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    for p in parts.split():
        _add(p)
    # Non-alphanum separators (hyphens, underscores, dots, colons, slashes)
    for p in re.split(r"[^a-zA-Z0-9]+", name):
        _add(p)
    if extras:
        for e in extras:
            _add(e)
    return tokens[:12]


def _line_of(text: str, pos: int) -> int:
    """0-indexed line number for character position ``pos``."""
    return text[:pos].count("\n")


def _find_block_end(lines: list[str], start: int, max_scan: int = 400) -> int:
    """Find the closing-brace line for a block that opens at or after ``start``."""
    depth = 0
    found_open = False
    for i in range(start, min(start + max_scan, len(lines))):
        line = lines[i]
        depth += line.count("{") - line.count("}")
        if line.count("{") > 0:
            found_open = True
        if found_open and depth <= 0:
            return i
    return min(start + 60, len(lines) - 1)


def _find_indent_block_end(lines: list[str], start: int, max_scan: int = 300) -> int:
    """Find end of an indented block (Python/YAML style) opened at ``start``."""
    if start >= len(lines):
        return start
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    end = start
    for i in range(start + 1, min(start + max_scan, len(lines))):
        stripped = lines[i].strip()
        if not stripped:
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if indent <= base_indent:
            return i - 1
        end = i
    return end


def _make_symbol(
    file_path: str,
    name: str,
    symbol_type: str,
    line_start: int,
    line_end: int,
    lines: list[str],
    confidence: float = 0.9,
    exported: bool = True,
    extra_kw: Optional[list[str]] = None,
    iac_type: Optional[str] = None,
    resource_type: Optional[str] = None,
    cdk_version: Optional[str] = None,
) -> dict:
    sym: dict = {
        "id": f"{file_path}::{name}",
        "name": name,
        "symbol_type": symbol_type,
        "line_start": line_start,
        "line_end": line_end,
        "body_hash": _body_hash(lines, line_start, min(line_end, len(lines) - 1)),
        "confidence": confidence,
        "exported": exported,
        "keywords": _name_keywords(name, extra_kw),
    }
    if iac_type is not None:
        sym["iac_type"] = iac_type
    if resource_type is not None:
        sym["resource_type"] = resource_type
    if cdk_version is not None:
        sym["cdk_version"] = cdk_version
    return sym


# ---------------------------------------------------------------------------
# 1. AWS CDK — TypeScript / JavaScript
# ---------------------------------------------------------------------------

# CDK version detection
_CDK_V2_IMPORT_TS_RE = re.compile(
    r"""(?:import|require)\s*(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)\s*(?:from\s*)?['"]aws-cdk-lib(?:/[^'"]*)?['"]""",
    re.MULTILINE,
)
_CDK_V1_IMPORT_TS_RE = re.compile(
    r"""(?:import|require)\s*(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)\s*(?:from\s*)?['"]@aws-cdk/(?:core|aws-[^'"]+)['"]""",
    re.MULTILINE,
)

# Stack class definitions
_CDK_STACK_TS_RE = re.compile(
    r"class\s+(\w+)\s+extends\s+(?:[\w.]*\.)?Stack\b",
    re.MULTILINE,
)

# Construct class definitions (Pulumi also uses similar, but here CDK-specific)
_CDK_CONSTRUCT_TS_RE = re.compile(
    r"class\s+(\w+)\s+extends\s+(?:[\w.]*\.)?Construct\b",
    re.MULTILINE,
)

# CDK App entry point
_CDK_APP_TS_RE = re.compile(
    r"new\s+(?:[\w.]*\.)?App\s*\(",
    re.MULTILINE,
)

# Resource constructs: new s3.Bucket(this, 'MyBucket', ...) or new Bucket(this, 'MyBucket')
_CDK_RESOURCE_TS_RE = re.compile(
    r"new\s+([\w]+(?:\.[\w]+)*)\s*\(\s*this\s*,\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)


def _cdk_version_ts(content: str) -> str:
    if _CDK_V2_IMPORT_TS_RE.search(content):
        return "v2"
    if _CDK_V1_IMPORT_TS_RE.search(content):
        return "v1"
    return "v2"  # default assumption for modern CDK


def _extract_cdk_ts(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()
    version = _cdk_version_ts(content)

    # Stack definitions → use_case
    for m in _CDK_STACK_TS_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path, name, "use_case", ln, end, lines,
                confidence=0.95, exported=True,
                extra_kw=["cdk", "stack", version],
                iac_type="cdk", resource_type="Stack", cdk_version=version,
            )
        )

    # Construct definitions → utility
    for m in _CDK_CONSTRUCT_TS_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path, name, "utility", ln, end, lines,
                confidence=0.9, exported=True,
                extra_kw=["cdk", "construct", version],
                iac_type="cdk", resource_type="Construct", cdk_version=version,
            )
        )

    # App entry → use_case (anonymous)
    for m in _CDK_APP_TS_RE.finditer(content):
        name = "CdkApp"
        if name in seen:
            break
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, name, "use_case", ln, ln, lines,
                confidence=0.8, exported=False,
                extra_kw=["cdk", "app", version],
                iac_type="cdk", resource_type="App", cdk_version=version,
            )
        )

    # Resource constructs → model
    for m in _CDK_RESOURCE_TS_RE.finditer(content):
        construct_type = m.group(1)   # e.g. "s3.Bucket" or "Lambda.Function"
        logical_name = m.group(2)     # e.g. "MyBucket"
        sym_name = f"{logical_name}_{construct_type.replace('.', '_')}"
        if sym_name in seen:
            continue
        # Skip Stack/Construct/App themselves
        base_type = construct_type.split(".")[-1]
        if base_type in ("Stack", "Construct", "App"):
            continue
        seen.add(sym_name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, sym_name, "model", ln, ln, lines,
                confidence=0.85, exported=False,
                extra_kw=["cdk", "resource", construct_type.lower(), version],
                iac_type="cdk", resource_type=construct_type, cdk_version=version,
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 2. AWS CDK — Python
# ---------------------------------------------------------------------------

_CDK_V2_IMPORT_PY_RE = re.compile(
    r"from\s+aws_cdk\s+import|import\s+aws_cdk\b",
    re.MULTILINE,
)
_CDK_V1_IMPORT_PY_RE = re.compile(
    r"from\s+aws_cdk\.aws_\w+|import\s+aws_cdk\.core\s+as|from\s+aws_cdk\.core",
    re.MULTILINE,
)

_CDK_STACK_PY_RE = re.compile(
    r"^class\s+(\w+)\s*\(\s*(?:[\w.]*\.)?Stack\s*\)\s*:",
    re.MULTILINE,
)
_CDK_CONSTRUCT_PY_RE = re.compile(
    r"^class\s+(\w+)\s*\(\s*(?:[\w.]*\.)?Construct\s*\)\s*:",
    re.MULTILINE,
)

# CDK Python resource pattern: service.ResourceClass(self, "LogicalName", ...)
# Captures module prefix like s3, ec2, ecs, lambda_, rds, dynamodb, sqs, sns, etc.
_CDK_RESOURCE_PY_RE = re.compile(
    r"(?:aws_\w+|s3|ec2|ecs|lambda_|rds|dynamodb|sqs|sns|iam|apigateway|"
    r"elbv2|cloudfront|cognito|secretsmanager|kms|route53|waf|glue|emr|"
    r"stepfunctions|events|logs|ssm|codecommit|codebuild|codepipeline|"
    r"elasticache|opensearch|kinesis|firehose|msk|appsync)"
    r"\.([\w]+)\s*\(\s*self\s*,\s*[\"']([^\"']+)[\"']",
    re.MULTILINE,
)

_CDK_APP_PY_RE = re.compile(
    r"(?:cdk\.|core\.)?App\s*\(",
    re.MULTILINE,
)


def _cdk_version_py(content: str) -> str:
    if _CDK_V2_IMPORT_PY_RE.search(content):
        return "v2"
    if _CDK_V1_IMPORT_PY_RE.search(content):
        return "v1"
    return "v2"


def _extract_cdk_py(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()
    version = _cdk_version_py(content)

    # Stack definitions → use_case
    for m in _CDK_STACK_PY_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_indent_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path, name, "use_case", ln, end, lines,
                confidence=0.95, exported=True,
                extra_kw=["cdk", "stack", version],
                iac_type="cdk", resource_type="Stack", cdk_version=version,
            )
        )

    # Construct definitions → utility
    for m in _CDK_CONSTRUCT_PY_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_indent_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path, name, "utility", ln, end, lines,
                confidence=0.9, exported=True,
                extra_kw=["cdk", "construct", version],
                iac_type="cdk", resource_type="Construct", cdk_version=version,
            )
        )

    # App entry → use_case
    for m in _CDK_APP_PY_RE.finditer(content):
        name = "CdkApp"
        if name in seen:
            break
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, name, "use_case", ln, ln, lines,
                confidence=0.8, exported=False,
                extra_kw=["cdk", "app", version],
                iac_type="cdk", resource_type="App", cdk_version=version,
            )
        )

    # Resource constructs → model
    for m in _CDK_RESOURCE_PY_RE.finditer(content):
        resource_class = m.group(1)   # e.g. "Bucket", "Function"
        logical_name = m.group(2)     # e.g. "MyBucket"
        sym_name = f"{logical_name}_{resource_class}"
        if sym_name in seen:
            continue
        if resource_class in ("Stack", "Construct", "App"):
            continue
        seen.add(sym_name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, sym_name, "model", ln, ln, lines,
                confidence=0.85, exported=False,
                extra_kw=["cdk", "resource", resource_class.lower(), version],
                iac_type="cdk", resource_type=resource_class, cdk_version=version,
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 3. AWS CloudFormation — YAML
# ---------------------------------------------------------------------------

_CFN_DETECT_RE = re.compile(
    r"AWSTemplateFormatVersion",
    re.MULTILINE,
)

# YAML resource block: two-space indent logical ID, then Type: AWS::...
_CFN_YAML_RESOURCE_RE = re.compile(
    r"^(?P<indent> {2,6})(?P<lid>\w+):\s*\n(?P=indent) {2}Type:\s*(?P<rtype>AWS::[A-Za-z0-9]+::[A-Za-z0-9]+)",
    re.MULTILINE,
)

# YAML parameters: under Parameters: section
_CFN_YAML_PARAM_RE = re.compile(
    r"^  (\w+):\s*\n    Type:\s*(String|Number|CommaDelimitedList|AWS::[A-Za-z0-9:]+)",
    re.MULTILINE,
)

# YAML outputs
_CFN_YAML_OUTPUT_RE = re.compile(
    r"^  (\w+):\s*\n    (?:Value|Description):",
    re.MULTILINE,
)

# Section anchors
_CFN_RESOURCES_SECTION_RE = re.compile(r"^Resources\s*:", re.MULTILINE)
_CFN_PARAMS_SECTION_RE = re.compile(r"^Parameters\s*:", re.MULTILINE)
_CFN_OUTPUTS_SECTION_RE = re.compile(r"^Outputs\s*:", re.MULTILINE)


def _extract_cfn_yaml(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # Top-level template symbol → use_case
    template_name = Path(file_path).stem
    symbols.append(
        _make_symbol(
            file_path, template_name, "use_case", 0, len(lines) - 1, lines,
            confidence=0.9, exported=True,
            extra_kw=["cfn", "template", "cloudformation"],
            iac_type="cfn",
        )
    )
    seen.add(template_name)

    # Resources
    for m in _CFN_YAML_RESOURCE_RE.finditer(content):
        logical_id = m.group("lid")
        resource_type = m.group("rtype")
        if logical_id in seen:
            continue
        seen.add(logical_id)
        ln = _line_of(content, m.start())
        # Scope resource to about 20 lines (YAML blocks vary)
        end = min(ln + 20, len(lines) - 1)
        # Nested stack is a use_case; other resources are model
        stype = "use_case" if resource_type == "AWS::CloudFormation::Stack" else "model"
        symbols.append(
            _make_symbol(
                file_path, logical_id, stype, ln, end, lines,
                confidence=0.9, exported=False,
                extra_kw=["cfn", "resource"] + resource_type.lower().split("::"),
                iac_type="cfn", resource_type=resource_type,
            )
        )

    # Parameters → utility
    param_section_m = _CFN_PARAMS_SECTION_RE.search(content)
    if param_section_m:
        param_start = param_section_m.end()
        # Find next top-level section
        next_section = re.search(r"^\w", content[param_start:], re.MULTILINE)
        param_region = content[param_start: param_start + (next_section.start() if next_section else len(content))]
        for pm in _CFN_YAML_PARAM_RE.finditer(param_region):
            pname = pm.group(1)
            ptype = pm.group(2)
            if pname in seen:
                continue
            seen.add(pname)
            ln = _line_of(content, param_section_m.end() + pm.start())
            symbols.append(
                _make_symbol(
                    file_path, pname, "utility", ln, ln, lines,
                    confidence=0.85, exported=False,
                    extra_kw=["cfn", "parameter", ptype.lower().split("::")[-1]],
                    iac_type="cfn", resource_type=f"Parameter:{ptype}",
                )
            )

    # Outputs → utility
    output_section_m = _CFN_OUTPUTS_SECTION_RE.search(content)
    if output_section_m:
        out_start = output_section_m.end()
        next_section = re.search(r"^\w", content[out_start:], re.MULTILINE)
        out_region = content[out_start: out_start + (next_section.start() if next_section else len(content))]
        for om in _CFN_YAML_OUTPUT_RE.finditer(out_region):
            oname = om.group(1)
            if oname in seen:
                continue
            seen.add(oname)
            ln = _line_of(content, output_section_m.end() + om.start())
            symbols.append(
                _make_symbol(
                    file_path, oname, "utility", ln, ln, lines,
                    confidence=0.8, exported=True,
                    extra_kw=["cfn", "output"],
                    iac_type="cfn", resource_type="Output",
                )
            )

    return symbols


# ---------------------------------------------------------------------------
# 4. AWS CloudFormation — JSON
# ---------------------------------------------------------------------------

_CFN_JSON_RESOURCE_RE = re.compile(
    r'"(\w+)"\s*:\s*\{\s*"Type"\s*:\s*"(AWS::[A-Za-z0-9]+::[A-Za-z0-9]+)"',
    re.MULTILINE | re.DOTALL,
)

_CFN_JSON_PARAM_RE = re.compile(
    r'"(\w+)"\s*:\s*\{[^}]{0,200}"Type"\s*:\s*"(String|Number|CommaDelimitedList)"',
    re.MULTILINE | re.DOTALL,
)

_CFN_JSON_OUTPUT_RE = re.compile(
    r'"(\w+)"\s*:\s*\{[^}]{0,300}"Value"\s*:',
    re.MULTILINE | re.DOTALL,
)

_CFN_JSON_DETECT_RE = re.compile(
    r'"AWSTemplateFormatVersion"',
    re.MULTILINE,
)

_CFN_JSON_RESOURCES_BLOCK_RE = re.compile(
    r'"Resources"\s*:\s*\{',
    re.MULTILINE,
)

_CFN_JSON_PARAMS_BLOCK_RE = re.compile(
    r'"Parameters"\s*:\s*\{',
    re.MULTILINE,
)

_CFN_JSON_OUTPUTS_BLOCK_RE = re.compile(
    r'"Outputs"\s*:\s*\{',
    re.MULTILINE,
)


def _json_section_content(content: str, section_re: re.Pattern) -> str:
    """Extract the raw text of a top-level JSON section (heuristic, brace-balanced)."""
    m = section_re.search(content)
    if not m:
        return ""
    start = m.end()
    depth = 1
    i = start
    while i < len(content) and depth > 0:
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
        i += 1
    return content[start:i]


def _extract_cfn_json(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # Top-level template symbol → use_case
    template_name = Path(file_path).stem
    symbols.append(
        _make_symbol(
            file_path, template_name, "use_case", 0, len(lines) - 1, lines,
            confidence=0.9, exported=True,
            extra_kw=["cfn", "template", "cloudformation"],
            iac_type="cfn",
        )
    )
    seen.add(template_name)

    # Resources section
    resources_block = _json_section_content(content, _CFN_JSON_RESOURCES_BLOCK_RE)
    resources_offset = _CFN_JSON_RESOURCES_BLOCK_RE.search(content)
    res_base = resources_offset.end() if resources_offset else 0

    for m in _CFN_JSON_RESOURCE_RE.finditer(resources_block):
        logical_id = m.group(1)
        resource_type = m.group(2)
        if logical_id in seen:
            continue
        seen.add(logical_id)
        ln = _line_of(content, res_base + m.start())
        end = min(ln + 15, len(lines) - 1)
        stype = "use_case" if resource_type == "AWS::CloudFormation::Stack" else "model"
        symbols.append(
            _make_symbol(
                file_path, logical_id, stype, ln, end, lines,
                confidence=0.9, exported=False,
                extra_kw=["cfn", "resource"] + resource_type.lower().split("::"),
                iac_type="cfn", resource_type=resource_type,
            )
        )

    # Parameters section
    params_block = _json_section_content(content, _CFN_JSON_PARAMS_BLOCK_RE)
    params_offset = _CFN_JSON_PARAMS_BLOCK_RE.search(content)
    par_base = params_offset.end() if params_offset else 0

    for m in _CFN_JSON_PARAM_RE.finditer(params_block):
        pname = m.group(1)
        ptype = m.group(2)
        if pname in seen:
            continue
        seen.add(pname)
        ln = _line_of(content, par_base + m.start())
        symbols.append(
            _make_symbol(
                file_path, pname, "utility", ln, ln, lines,
                confidence=0.85, exported=False,
                extra_kw=["cfn", "parameter", ptype.lower()],
                iac_type="cfn", resource_type=f"Parameter:{ptype}",
            )
        )

    # Outputs section
    outputs_block = _json_section_content(content, _CFN_JSON_OUTPUTS_BLOCK_RE)
    outputs_offset = _CFN_JSON_OUTPUTS_BLOCK_RE.search(content)
    out_base = outputs_offset.end() if outputs_offset else 0

    for m in _CFN_JSON_OUTPUT_RE.finditer(outputs_block):
        oname = m.group(1)
        if oname in seen:
            continue
        seen.add(oname)
        ln = _line_of(content, out_base + m.start())
        symbols.append(
            _make_symbol(
                file_path, oname, "utility", ln, ln, lines,
                confidence=0.8, exported=True,
                extra_kw=["cfn", "output"],
                iac_type="cfn", resource_type="Output",
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 5. Pulumi — TypeScript / JavaScript
# ---------------------------------------------------------------------------

_PULUMI_IMPORT_TS_RE = re.compile(
    r"""(?:import|require)\s*(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)\s*(?:from\s*)?['"]@pulumi/(?:pulumi|aws|azure|gcp|kubernetes)['""]""",
    re.MULTILINE,
)

_PULUMI_COMPONENT_TS_RE = re.compile(
    r"class\s+(\w+)\s+extends\s+pulumi\.ComponentResource\b",
    re.MULTILINE,
)

_PULUMI_STACK_REF_TS_RE = re.compile(
    r"""new\s+pulumi\.StackReference\s*\(\s*[`'"]([^`'"]+)[`'"]""",
    re.MULTILINE,
)

_PULUMI_CONFIG_TS_RE = re.compile(
    r"new\s+pulumi\.Config\s*\(",
    re.MULTILINE,
)

# Generic resource: new aws.s3.Bucket("myBucket", ...)
# Covers template literals, single/double quotes
_PULUMI_RESOURCE_TS_RE = re.compile(
    r"""new\s+([\w]+(?:\.[\w]+)+)\s*\(\s*[`'"]([^`'"]+)[`'"]""",
    re.MULTILINE,
)

_PULUMI_EXPORT_TS_RE = re.compile(
    r"""^export\s+const\s+(\w+)\s*=|pulumi\.export\s*\(\s*[`'"]([^`'"]+)[`'"]""",
    re.MULTILINE,
)


def _extract_pulumi_ts(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # ComponentResource → use_case
    for m in _PULUMI_COMPONENT_TS_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path, name, "use_case", ln, end, lines,
                confidence=0.95, exported=True,
                extra_kw=["pulumi", "component"],
                iac_type="pulumi",
            )
        )

    # Config → utility
    for m in _PULUMI_CONFIG_TS_RE.finditer(content):
        name = "PulumiConfig"
        if name in seen:
            break
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, name, "utility", ln, ln, lines,
                confidence=0.8, exported=False,
                extra_kw=["pulumi", "config"],
                iac_type="pulumi", resource_type="Config",
            )
        )

    # StackReference → utility
    for m in _PULUMI_STACK_REF_TS_RE.finditer(content):
        ref_name = m.group(1)
        sym_name = f"StackRef_{ref_name.replace('/', '_').replace('.', '_')}"
        if sym_name in seen:
            continue
        seen.add(sym_name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, sym_name, "utility", ln, ln, lines,
                confidence=0.9, exported=False,
                extra_kw=["pulumi", "stackreference", ref_name],
                iac_type="pulumi", resource_type="StackReference",
            )
        )

    # Resources → model
    for m in _PULUMI_RESOURCE_TS_RE.finditer(content):
        resource_type = m.group(1)
        logical_name = m.group(2)
        # Skip non-Pulumi constructors (filter by known provider prefixes)
        provider_prefix = resource_type.split(".")[0].lower()
        if provider_prefix not in (
            "aws", "azure", "gcp", "kubernetes", "k8s", "pulumi",
            "awsx", "eks", "rds", "s3", "ec2", "ecs", "lambda",
        ):
            continue
        if resource_type.endswith(".ComponentResource") or resource_type.endswith(".StackReference"):
            continue
        sym_name = f"{logical_name}_{resource_type.replace('.', '_')}"
        if sym_name in seen:
            continue
        seen.add(sym_name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, sym_name, "model", ln, ln, lines,
                confidence=0.85, exported=False,
                extra_kw=["pulumi", "resource", resource_type.lower()],
                iac_type="pulumi", resource_type=resource_type,
            )
        )

    # Exports → utility
    for m in _PULUMI_EXPORT_TS_RE.finditer(content):
        name = m.group(1) or m.group(2)
        if not name or name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, name, "utility", ln, ln, lines,
                confidence=0.8, exported=True,
                extra_kw=["pulumi", "export"],
                iac_type="pulumi", resource_type="Export",
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 6. Pulumi — Python
# ---------------------------------------------------------------------------

_PULUMI_IMPORT_PY_RE = re.compile(
    r"^import\s+pulumi(?:\s|$)|from\s+pulumi(?:_\w+)?\s+import|import\s+pulumi_\w+",
    re.MULTILINE,
)

_PULUMI_COMPONENT_PY_RE = re.compile(
    r"^class\s+(\w+)\s*\(\s*pulumi\.ComponentResource\s*\)\s*:",
    re.MULTILINE,
)

_PULUMI_RESOURCE_PY_RE = re.compile(
    r"(\w+)\s*=\s*(?:aws|pulumi_aws|azure|pulumi_azure|gcp|pulumi_gcp|kubernetes|pulumi_kubernetes)\s*\.\s*\w+\s*\.\s*\w+\s*\(\s*[\"']([^\"']+)[\"']",
    re.MULTILINE,
)

_PULUMI_CONFIG_PY_RE = re.compile(
    r"(?:config|cfg)\s*=\s*pulumi\.Config\s*\(",
    re.MULTILINE,
)

_PULUMI_EXPORT_PY_RE = re.compile(
    r"""pulumi\.export\s*\(\s*['""]([^'"]+)['""]""",
    re.MULTILINE,
)

_PULUMI_STACKREF_PY_RE = re.compile(
    r"""(?:pulumi\.)?StackReference\s*\(\s*['""]([^'"]+)['""]""",
    re.MULTILINE,
)


def _extract_pulumi_py(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # ComponentResource → use_case
    for m in _PULUMI_COMPONENT_PY_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_indent_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path, name, "use_case", ln, end, lines,
                confidence=0.95, exported=True,
                extra_kw=["pulumi", "component"],
                iac_type="pulumi",
            )
        )

    # Config → utility
    for m in _PULUMI_CONFIG_PY_RE.finditer(content):
        name = "PulumiConfig"
        if name in seen:
            break
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, name, "utility", ln, ln, lines,
                confidence=0.8, exported=False,
                extra_kw=["pulumi", "config"],
                iac_type="pulumi", resource_type="Config",
            )
        )

    # StackReference → utility
    for m in _PULUMI_STACKREF_PY_RE.finditer(content):
        ref_name = m.group(1)
        sym_name = f"StackRef_{ref_name.replace('/', '_').replace('.', '_')}"
        if sym_name in seen:
            continue
        seen.add(sym_name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, sym_name, "utility", ln, ln, lines,
                confidence=0.9, exported=False,
                extra_kw=["pulumi", "stackreference", ref_name],
                iac_type="pulumi", resource_type="StackReference",
            )
        )

    # Resources → model
    for m in _PULUMI_RESOURCE_PY_RE.finditer(content):
        var_name = m.group(1)
        logical_name = m.group(2)
        sym_name = f"{logical_name}_{var_name}"
        if sym_name in seen:
            continue
        seen.add(sym_name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, sym_name, "model", ln, ln, lines,
                confidence=0.85, exported=False,
                extra_kw=["pulumi", "resource", var_name.lower()],
                iac_type="pulumi",
            )
        )

    # Exports → utility
    for m in _PULUMI_EXPORT_PY_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, name, "utility", ln, ln, lines,
                confidence=0.8, exported=True,
                extra_kw=["pulumi", "export"],
                iac_type="pulumi", resource_type="Export",
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 7. Azure Bicep (.bicep)
# ---------------------------------------------------------------------------

# resource symbolic_name 'type@api-version' = { ... }
_BICEP_RESOURCE_RE = re.compile(
    r"^resource\s+(\w+)\s+'([A-Za-z][A-Za-z0-9./]+)@(\d{4}-\d{2}-\d{2}[^']*)'",
    re.MULTILINE,
)

# module name 'path/to/file.bicep' = { ... }
_BICEP_MODULE_RE = re.compile(
    r"^module\s+(\w+)\s+'([^']+\.bicep)'",
    re.MULTILINE,
)

# param name type [= default]
_BICEP_PARAM_RE = re.compile(
    r"^param\s+(\w+)\s+(string|int|bool|object|array|secureString|securestring)\s*(?:=.*)?$",
    re.MULTILINE | re.IGNORECASE,
)

# output name type = expression
_BICEP_OUTPUT_RE = re.compile(
    r"^output\s+(\w+)\s+\w+\s*=",
    re.MULTILINE,
)

# targetScope = 'subscription' | 'resourceGroup' | 'managementGroup' | 'tenant'
_BICEP_SCOPE_RE = re.compile(
    r"^targetScope\s*=\s*'(\w+)'",
    re.MULTILINE,
)

# var declarations
_BICEP_VAR_RE = re.compile(
    r"^var\s+(\w+)\s*=",
    re.MULTILINE,
)


def _extract_bicep(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # Determine scope
    scope_m = _BICEP_SCOPE_RE.search(content)
    scope_val = scope_m.group(1) if scope_m else "resourceGroup"

    # Top-level file → use_case if targeting subscription/management/tenant scope
    # otherwise we emit a use_case for resource-group level deployments too
    tpl_name = Path(file_path).stem
    symbols.append(
        _make_symbol(
            file_path, tpl_name, "use_case", 0, len(lines) - 1, lines,
            confidence=0.9, exported=True,
            extra_kw=["bicep", "template", scope_val],
            iac_type="bicep", resource_type=f"Deployment:{scope_val}",
        )
    )
    seen.add(tpl_name)

    # Resources → model
    for m in _BICEP_RESOURCE_RE.finditer(content):
        sym_name = m.group(1)
        resource_type = m.group(2)
        api_version = m.group(3)
        if sym_name in seen:
            continue
        seen.add(sym_name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path, sym_name, "model", ln, end, lines,
                confidence=0.95, exported=False,
                extra_kw=["bicep", "resource"] + resource_type.lower().replace("/", "::").split("::"),
                iac_type="bicep", resource_type=f"{resource_type}@{api_version}",
            )
        )

    # Modules → utility (child deployment)
    for m in _BICEP_MODULE_RE.finditer(content):
        mod_name = m.group(1)
        mod_path = m.group(2)
        if mod_name in seen:
            continue
        seen.add(mod_name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path, mod_name, "utility", ln, end, lines,
                confidence=0.9, exported=False,
                extra_kw=["bicep", "module", Path(mod_path).stem],
                iac_type="bicep", resource_type=f"Module:{mod_path}",
            )
        )

    # Params → utility
    for m in _BICEP_PARAM_RE.finditer(content):
        pname = m.group(1)
        ptype = m.group(2)
        if pname in seen:
            continue
        seen.add(pname)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, pname, "utility", ln, ln, lines,
                confidence=0.85, exported=False,
                extra_kw=["bicep", "param", ptype.lower()],
                iac_type="bicep", resource_type=f"Param:{ptype}",
            )
        )

    # Outputs → utility
    for m in _BICEP_OUTPUT_RE.finditer(content):
        oname = m.group(1)
        if oname in seen:
            continue
        seen.add(oname)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path, oname, "utility", ln, ln, lines,
                confidence=0.8, exported=True,
                extra_kw=["bicep", "output"],
                iac_type="bicep", resource_type="Output",
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 8. Azure ARM JSON
# ---------------------------------------------------------------------------

_ARM_DETECT_RE = re.compile(
    r'''"?\$schema"?\s*:\s*"[^"]*schema\.management\.azure\.com[^"]*"''',
    re.MULTILINE,
)

# Resource type: "type": "Microsoft.Storage/storageAccounts"
_ARM_RESOURCE_TYPE_RE = re.compile(
    r'''"type"\s*:\s*"([A-Za-z][A-Za-z0-9.]+/[A-Za-z][A-Za-z0-9/]*)"''',
    re.MULTILINE,
)

# Resource name (typically 2-5 lines after type in same resource block)
_ARM_RESOURCE_NAME_RE = re.compile(
    r'''"name"\s*:\s*"([^"]+)"''',
    re.MULTILINE,
)

# DependsOn: "dependsOn": ["res1", "res2"]
_ARM_DEPENDSON_RE = re.compile(
    r""""dependsOn"\s*:\s*\[([^\]]+)\]""",
    re.MULTILINE | re.DOTALL,
)

# ARM parameter
_ARM_PARAM_RE = re.compile(
    r'''"(\w+)"\s*:\s*\{[^}]{0,200}"type"\s*:\s*"(string|int|bool|object|array|securestring)"''',
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

_ARM_RESOURCES_BLOCK_RE = re.compile(
    r'"resources"\s*:\s*\[',
    re.MULTILINE,
)

_ARM_PARAMS_BLOCK_RE = re.compile(
    r'"parameters"\s*:\s*\{',
    re.MULTILINE,
)


def _extract_arm_json(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # Top-level template → use_case
    tpl_name = Path(file_path).stem
    symbols.append(
        _make_symbol(
            file_path, tpl_name, "use_case", 0, len(lines) - 1, lines,
            confidence=0.9, exported=True,
            extra_kw=["arm", "template", "azure"],
            iac_type="bicep",  # ARM is the compiled form of Bicep
        )
    )
    seen.add(tpl_name)

    # Resources: scan "resources" array for type+name pairs
    # Strategy: find each "type" occurrence, look for "name" within 8 lines
    for m_type in _ARM_RESOURCE_TYPE_RE.finditer(content):
        resource_type = m_type.group(1)
        # Look for "name" key within next ~300 chars
        snippet = content[m_type.start() : m_type.start() + 400]
        m_name = _ARM_RESOURCE_NAME_RE.search(snippet)
        res_name = m_name.group(1) if m_name else resource_type.split("/")[-1]
        # Sanitize ARM expression names like "[concat(...)]"
        if res_name.startswith("["):
            res_name = re.sub(r"[^\w]", "_", res_name)[:30]

        sym_name = f"{res_name}_{resource_type.replace('/', '_').replace('.', '_')}"
        if sym_name in seen:
            continue
        seen.add(sym_name)
        ln = _line_of(content, m_type.start())
        end = min(ln + 20, len(lines) - 1)
        symbols.append(
            _make_symbol(
                file_path, sym_name, "model", ln, end, lines,
                confidence=0.9, exported=False,
                extra_kw=["arm", "resource"] + resource_type.lower().split("/"),
                iac_type="bicep", resource_type=resource_type,
            )
        )

    # Parameters → utility
    params_section = _json_section_content(content, _ARM_PARAMS_BLOCK_RE)
    params_offset = _ARM_PARAMS_BLOCK_RE.search(content)
    par_base = params_offset.end() if params_offset else 0
    for pm in _ARM_PARAM_RE.finditer(params_section):
        pname = pm.group(1)
        ptype = pm.group(2)
        if pname in seen:
            continue
        seen.add(pname)
        ln = _line_of(content, par_base + pm.start())
        symbols.append(
            _make_symbol(
                file_path, pname, "utility", ln, ln, lines,
                confidence=0.85, exported=False,
                extra_kw=["arm", "parameter", ptype.lower()],
                iac_type="bicep", resource_type=f"Parameter:{ptype}",
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

_CDK_TS_DETECT_TOKENS = (
    "from 'aws-cdk-lib'",
    'from "aws-cdk-lib"',
    "from 'aws-cdk-lib/",
    'from "aws-cdk-lib/',
    "from '@aws-cdk/",
    'from "@aws-cdk/',
    "require('aws-cdk-lib')",
    'require("aws-cdk-lib")',
    "require('@aws-cdk/",
    'require("@aws-cdk/',
)

_CDK_PY_DETECT_TOKENS = (
    "from aws_cdk import",
    "import aws_cdk",
    "from aws_cdk.aws_",
    "aws_cdk.core",
)

_CDK_JAVA_DETECT_TOKENS = (
    "software.amazon.awscdk",
    "import software.constructs",
)

_CFN_DETECT_TOKENS = (
    "AWSTemplateFormatVersion",
    '"AWSTemplateFormatVersion"',
)

_PULUMI_TS_DETECT_TOKENS = (
    "from '@pulumi/",
    'from "@pulumi/',
    "require('@pulumi/",
    'require("@pulumi/',
)

_PULUMI_PY_DETECT_TOKENS = (
    "import pulumi",
    "from pulumi import",
    "from pulumi_",
    "import pulumi_aws",
    "import pulumi_azure",
    "import pulumi_gcp",
)

_BICEP_RESOURCE_DETECT_RE = re.compile(
    r"resource\s+\w+\s+'[A-Za-z]",
    re.MULTILINE,
)

_ARM_DETECT_TOKENS = (
    "schema.management.azure.com",
)


def _is_cdk(content4k: str, ext: str) -> bool:
    if ext in (".ts", ".js"):
        return any(t in content4k for t in _CDK_TS_DETECT_TOKENS)
    if ext == ".py":
        return any(t in content4k for t in _CDK_PY_DETECT_TOKENS)
    if ext in (".java",):
        return any(t in content4k for t in _CDK_JAVA_DETECT_TOKENS)
    return False


def _is_cfn(content4k: str, ext: str) -> bool:
    return any(t in content4k for t in _CFN_DETECT_TOKENS)


def _is_pulumi(content4k: str, ext: str) -> bool:
    if ext in (".ts", ".js"):
        return any(t in content4k for t in _PULUMI_TS_DETECT_TOKENS)
    if ext == ".py":
        return any(t in content4k for t in _PULUMI_PY_DETECT_TOKENS)
    return False


def _is_bicep(content4k: str, ext: str, file_path: str) -> bool:
    if ext == ".bicep":
        return True
    return bool(_BICEP_RESOURCE_DETECT_RE.search(content4k))


def _is_arm(content4k: str, ext: str) -> bool:
    return any(t in content4k for t in _ARM_DETECT_TOKENS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def supports_iac_extended(content: str, file_path: str, ext: str) -> bool:
    """Fast pre-filter — True if this file looks like CDK / CFN / Pulumi / Bicep.

    Checks only the first 4KB for signature patterns. Must be cheap.
    """
    if ext not in IAC_EXT_EXTS:
        return False

    # .bicep always qualifies
    if ext == ".bicep":
        return True

    c4k = content[:4096]

    return (
        _is_cdk(c4k, ext)
        or _is_cfn(c4k, ext)
        or _is_pulumi(c4k, ext)
        or _is_bicep(c4k, ext, file_path)
        or _is_arm(c4k, ext)
    )


def extract_iac_extended_symbols(content: str, file_path: str, ext: str) -> list[dict]:
    """Main dispatcher — returns IaC symbols for all frameworks present in the file.

    Returns a list of symbol dicts with the standard GrapeRoot Pro format,
    augmented with optional iac_type, resource_type, and cdk_version fields.
    """
    if ext not in IAC_EXT_EXTS:
        return []

    symbols: list[dict] = []
    c4k = content[:4096]

    # CDK (TypeScript/JavaScript)
    if ext in (".ts", ".js") and _is_cdk(c4k, ext):
        symbols.extend(_extract_cdk_ts(content, file_path))

    # CDK Python
    if ext == ".py" and _is_cdk(c4k, ext):
        symbols.extend(_extract_cdk_py(content, file_path))

    # Pulumi TypeScript/JavaScript (only if not already CDK — check via markers)
    if ext in (".ts", ".js") and _is_pulumi(c4k, ext) and not _is_cdk(c4k, ext):
        symbols.extend(_extract_pulumi_ts(content, file_path))
    elif ext in (".ts", ".js") and _is_pulumi(c4k, ext):
        # Could be mixed; run Pulumi extractor regardless (dedup at end)
        symbols.extend(_extract_pulumi_ts(content, file_path))

    # Pulumi Python
    if ext == ".py" and _is_pulumi(c4k, ext):
        symbols.extend(_extract_pulumi_py(content, file_path))

    # CloudFormation YAML
    if ext in (".yml", ".yaml") and _is_cfn(c4k, ext):
        symbols.extend(_extract_cfn_yaml(content, file_path))

    # CloudFormation / ARM JSON
    if ext in (".json", ".template"):
        if _is_cfn(c4k, ext):
            symbols.extend(_extract_cfn_json(content, file_path))
        elif _is_arm(c4k, ext):
            symbols.extend(_extract_arm_json(content, file_path))

    # Bicep
    if ext == ".bicep":
        symbols.extend(_extract_bicep(content, file_path))

    # ARM JSON (if .json with Azure schema and not already detected as CFN)
    if ext == ".json" and _is_arm(c4k, ext) and not _is_cfn(c4k, ext):
        symbols.extend(_extract_arm_json(content, file_path))

    # Deduplicate by id (in case multiple extractors match the same file)
    seen_ids: set[str] = set()
    unique: list[dict] = []
    for s in symbols:
        if s["id"] not in seen_ids:
            seen_ids.add(s["id"])
            unique.append(s)
    return unique


def parse_iac_extended_imports(content: str, file_id: str, ext: str) -> list[dict]:
    """Return edge dicts representing IaC-level resource relationships.

    Each edge dict:
    {
        "from_id":    str,   # source symbol id (file_id::SymbolName)
        "to_id":      str,   # target symbol id (may be unresolved name)
        "relation":   str,   # "references" | "contains" | "needs"
        "confidence": float,
    }
    """
    if ext not in IAC_EXT_EXTS:
        return []

    edges: list[dict] = []
    c4k = content[:4096]

    # CDK TypeScript/JavaScript: stack → resource edges
    if ext in (".ts", ".js") and _is_cdk(c4k, ext):
        edges.extend(_parse_cdk_ts_edges(content, file_id))

    # CDK Python: stack → resource edges
    if ext == ".py" and _is_cdk(c4k, ext):
        edges.extend(_parse_cdk_py_edges(content, file_id))

    # CloudFormation YAML: DependsOn edges
    if ext in (".yml", ".yaml") and _is_cfn(c4k, ext):
        edges.extend(_parse_cfn_yaml_edges(content, file_id))

    # CloudFormation JSON: DependsOn edges
    if ext in (".json", ".template") and _is_cfn(c4k, ext):
        edges.extend(_parse_cfn_json_edges(content, file_id))

    # Pulumi TypeScript: StackReference edges
    if ext in (".ts", ".js") and _is_pulumi(c4k, ext):
        edges.extend(_parse_pulumi_ts_edges(content, file_id))

    # Pulumi Python
    if ext == ".py" and _is_pulumi(c4k, ext):
        edges.extend(_parse_pulumi_py_edges(content, file_id))

    # Bicep: module → parent edges
    if ext == ".bicep":
        edges.extend(_parse_bicep_edges(content, file_id))

    # ARM: dependsOn edges
    if ext == ".json" and _is_arm(c4k, ext):
        edges.extend(_parse_arm_edges(content, file_id))

    return edges


# ---------------------------------------------------------------------------
# Edge parsers
# ---------------------------------------------------------------------------

def _parse_cdk_ts_edges(content: str, file_id: str) -> list[dict]:
    """Emit stack → resource (references) and construct → nested (contains) edges."""
    edges: list[dict] = []

    # Find all Stack names in this file
    stack_names = [m.group(1) for m in _CDK_STACK_TS_RE.finditer(content)]

    # For each resource new X(this, 'Name'), link from the enclosing stack
    for m in _CDK_RESOURCE_TS_RE.finditer(content):
        construct_type = m.group(1)
        logical_name = m.group(2)
        base_type = construct_type.split(".")[-1]
        if base_type in ("Stack", "Construct", "App"):
            continue
        from_stack = stack_names[0] if stack_names else "UnknownStack"
        sym_name = f"{logical_name}_{construct_type.replace('.', '_')}"
        edges.append({
            "from_id": f"{file_id}::{from_stack}",
            "to_id": f"{file_id}::{sym_name}",
            "relation": "references",
            "confidence": 0.85,
        })

    # Construct extends → contains: CDK constructs are contained by their stack
    for m in _CDK_CONSTRUCT_TS_RE.finditer(content):
        construct_name = m.group(1)
        from_stack = stack_names[0] if stack_names else "UnknownStack"
        edges.append({
            "from_id": f"{file_id}::{from_stack}",
            "to_id": f"{file_id}::{construct_name}",
            "relation": "contains",
            "confidence": 0.8,
        })

    return edges


def _parse_cdk_py_edges(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    stack_names = [m.group(1) for m in _CDK_STACK_PY_RE.finditer(content)]

    for m in _CDK_RESOURCE_PY_RE.finditer(content):
        resource_class = m.group(1)
        logical_name = m.group(2)
        if resource_class in ("Stack", "Construct", "App"):
            continue
        from_stack = stack_names[0] if stack_names else "UnknownStack"
        sym_name = f"{logical_name}_{resource_class}"
        edges.append({
            "from_id": f"{file_id}::{from_stack}",
            "to_id": f"{file_id}::{sym_name}",
            "relation": "references",
            "confidence": 0.85,
        })

    for m in _CDK_CONSTRUCT_PY_RE.finditer(content):
        construct_name = m.group(1)
        from_stack = stack_names[0] if stack_names else "UnknownStack"
        edges.append({
            "from_id": f"{file_id}::{from_stack}",
            "to_id": f"{file_id}::{construct_name}",
            "relation": "contains",
            "confidence": 0.8,
        })

    return edges


_CFN_YAML_DEPENDSON_RE = re.compile(
    r"DependsOn\s*:\s*(?:\n\s+-\s+(\w+)|\[([^\]]+)\]|(\w+))",
    re.MULTILINE,
)


def _parse_cfn_yaml_edges(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    lines = content.splitlines()

    for m in _CFN_YAML_RESOURCE_RE.finditer(content):
        logical_id = m.group("lid")
        resource_type = m.group("rtype")
        ln = _line_of(content, m.start())
        # Scan nearby lines for DependsOn
        scan_end = min(ln + 25, len(lines))
        snippet = "\n".join(lines[ln:scan_end])
        for dep_m in _CFN_YAML_DEPENDSON_RE.finditer(snippet):
            raw = dep_m.group(1) or dep_m.group(2) or dep_m.group(3) or ""
            for dep_name in re.findall(r"\w+", raw):
                if dep_name and dep_name != logical_id:
                    edges.append({
                        "from_id": f"{file_id}::{logical_id}",
                        "to_id": f"{file_id}::{dep_name}",
                        "relation": "needs",
                        "confidence": 0.9,
                    })

    return edges


def _parse_cfn_json_edges(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []

    resources_block = _json_section_content(content, _CFN_JSON_RESOURCES_BLOCK_RE)
    res_offset = _CFN_JSON_RESOURCES_BLOCK_RE.search(content)
    if not res_offset:
        return edges

    for m_type in _CFN_JSON_RESOURCE_RE.finditer(resources_block):
        logical_id = m_type.group(1)
        # Look for dependsOn within ~500 chars after this resource
        snippet = resources_block[m_type.start(): m_type.start() + 600]
        for dep_m in _ARM_DEPENDSON_RE.finditer(snippet):
            raw = dep_m.group(1)
            for dep_name in re.findall(r'"(\w+)"', raw):
                if dep_name and dep_name != logical_id:
                    edges.append({
                        "from_id": f"{file_id}::{logical_id}",
                        "to_id": f"{file_id}::{dep_name}",
                        "relation": "needs",
                        "confidence": 0.9,
                    })

    return edges


def _parse_pulumi_ts_edges(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []

    # StackReference → references edge
    for m in _PULUMI_STACK_REF_TS_RE.finditer(content):
        ref_name = m.group(1)
        sym_name = f"StackRef_{ref_name.replace('/', '_').replace('.', '_')}"
        edges.append({
            "from_id": file_id,
            "to_id": f"{ref_name}::root",
            "relation": "references",
            "confidence": 0.8,
        })

    return edges


def _parse_pulumi_py_edges(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []

    for m in _PULUMI_STACKREF_PY_RE.finditer(content):
        ref_name = m.group(1)
        edges.append({
            "from_id": file_id,
            "to_id": f"{ref_name}::root",
            "relation": "references",
            "confidence": 0.8,
        })

    return edges


def _parse_bicep_edges(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    tpl_name = file_id.split("::")[-1] if "::" in file_id else Path(file_id).stem

    # Parent → module (contains)
    for m in _BICEP_MODULE_RE.finditer(content):
        mod_name = m.group(1)
        edges.append({
            "from_id": f"{file_id}::{tpl_name}",
            "to_id": f"{file_id}::{mod_name}",
            "relation": "contains",
            "confidence": 0.9,
        })

    return edges


_ARM_DEPENDSON_ITEM_RE = re.compile(r'"([^"]+)"')


def _parse_arm_edges(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []

    # For each resource type/name pair, find its dependsOn
    for m_type in _ARM_RESOURCE_TYPE_RE.finditer(content):
        resource_type = m_type.group(1)
        snippet = content[m_type.start(): m_type.start() + 600]
        m_name = _ARM_RESOURCE_NAME_RE.search(snippet)
        res_name = m_name.group(1) if m_name else resource_type.split("/")[-1]
        if res_name.startswith("["):
            res_name = re.sub(r"[^\w]", "_", res_name)[:30]
        sym_name = f"{res_name}_{resource_type.replace('/', '_').replace('.', '_')}"

        # Look for dependsOn in same resource block
        for dep_m in _ARM_DEPENDSON_RE.finditer(snippet):
            raw = dep_m.group(1)
            for dep_item in _ARM_DEPENDSON_ITEM_RE.finditer(raw):
                dep_ref = dep_item.group(1)
                # Skip ARM expressions
                if not dep_ref.startswith("["):
                    edges.append({
                        "from_id": f"{file_id}::{sym_name}",
                        "to_id": f"{file_id}::{dep_ref}",
                        "relation": "needs",
                        "confidence": 0.85,
                    })

    return edges


# ---------------------------------------------------------------------------
# MCP TOOL HELPERS
# ---------------------------------------------------------------------------


def get_iac_extended_summary(project_root: str) -> dict:
    """Walk ``project_root``, extract IaC symbols from matching files, return summary.

    Returns:
        {
            "ok": bool,
            "total_resources": int,
            "by_type": {"cdk": int, "cfn": int, "pulumi": int, "bicep": int},
            "resources": [list of symbol dicts],
        }
    """
    try:
        root = Path(project_root)
        by_type: dict[str, int] = {"cdk": 0, "cfn": 0, "pulumi": 0, "bicep": 0}
        resources: list[dict] = []

        for dirpath, dirnames, filenames in os.walk(root):
            # Skip common ignore dirs
            dirnames[:] = [
                d for d in dirnames
                if d not in (
                    "node_modules", ".git", "__pycache__", ".venv", "venv",
                    "dist", "build", ".terraform", "cdk.out",
                )
            ]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()
                if ext not in IAC_EXT_EXTS:
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if not supports_iac_extended(content, str(fpath), ext):
                    continue
                syms = extract_iac_extended_symbols(content, str(fpath), ext)
                resources.extend(syms)
                for s in syms:
                    iac = s.get("iac_type", "")
                    if iac in by_type:
                        by_type[iac] += 1

        return {
            "ok": True,
            "total_resources": len(resources),
            "by_type": by_type,
            "resources": resources,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "total_resources": 0, "by_type": {}, "resources": []}


def get_cdk_stacks(project_root: str) -> dict:
    """Return all CDK Stack definitions with their resources.

    Returns:
        {
            "ok": bool,
            "stacks": [{"stack_name": str, "file": str, "version": str, "resources": [...]}]
        }
    """
    try:
        root = Path(project_root)
        stacks: list[dict] = []

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("node_modules", ".git", "__pycache__", ".venv", "cdk.out")
            ]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()
                if ext not in IAC_CDK_EXTS:
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                c4k = content[:4096]
                if not _is_cdk(c4k, ext):
                    continue
                syms = extract_iac_extended_symbols(content, str(fpath), ext)
                stack_syms = [s for s in syms if s.get("resource_type") == "Stack"]
                resource_syms = [s for s in syms if s.get("symbol_type") == "model"]
                for ss in stack_syms:
                    stacks.append({
                        "stack_name": ss["name"],
                        "file": str(fpath),
                        "version": ss.get("cdk_version", "unknown"),
                        "line_start": ss["line_start"],
                        "resources": resource_syms,
                    })

        return {"ok": True, "stacks": stacks}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "stacks": []}


def get_cfn_resources(project_root: str) -> dict:
    """Return all CloudFormation resources grouped by template file.

    Returns:
        {
            "ok": bool,
            "templates": [{"file": str, "resources": [...]}]
        }
    """
    try:
        root = Path(project_root)
        templates: list[dict] = []

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("node_modules", ".git", "__pycache__", "cdk.out")
            ]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()
                if ext not in IAC_CFN_EXTS:
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if not _is_cfn(content[:4096], ext):
                    continue
                syms = extract_iac_extended_symbols(content, str(fpath), ext)
                resources = [s for s in syms if s.get("iac_type") == "cfn"]
                if resources:
                    templates.append({"file": str(fpath), "resources": resources})

        return {"ok": True, "templates": templates}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "templates": []}


def get_pulumi_resources(project_root: str) -> dict:
    """Return all Pulumi resources grouped by component file.

    Returns:
        {
            "ok": bool,
            "components": [{"file": str, "component": str, "resources": [...]}]
        }
    """
    try:
        root = Path(project_root)
        components: list[dict] = []

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("node_modules", ".git", "__pycache__", ".venv")
            ]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()
                if ext not in IAC_PULUMI_EXTS:
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if not _is_pulumi(content[:4096], ext):
                    continue
                syms = extract_iac_extended_symbols(content, str(fpath), ext)
                pulumi_syms = [s for s in syms if s.get("iac_type") == "pulumi"]
                component_syms = [s for s in pulumi_syms if s["symbol_type"] == "use_case"]
                resource_syms = [s for s in pulumi_syms if s["symbol_type"] == "model"]
                comp_name = component_syms[0]["name"] if component_syms else Path(fname).stem
                if pulumi_syms:
                    components.append({
                        "file": str(fpath),
                        "component": comp_name,
                        "resources": resource_syms,
                    })

        return {"ok": True, "components": components}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "components": []}


# ---------------------------------------------------------------------------
# Inline test suite
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _PASS = "[PASS]"
    _FAIL = "[FAIL]"

    def _check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"{_PASS}  {label}")
        else:
            msg = f"{_FAIL}  {label}"
            if detail:
                msg += f"  — {detail}"
            raise AssertionError(msg)

    # ── Test 1: CDK v1 TypeScript Stack detection ─────────────────────────────
    cdk_v1_ts = """
import * as cdk from '@aws-cdk/core'
import * as s3 from '@aws-cdk/aws-s3'

export class MyV1Stack extends cdk.Stack {
  constructor(scope: cdk.Construct, id: string) {
    super(scope, id)
    new s3.Bucket(this, 'MyBucket', { versioned: true })
  }
}

const app = new cdk.App()
new MyV1Stack(app, 'MyV1Stack')
"""
    assert supports_iac_extended(cdk_v1_ts, "lib/stack.ts", ".ts"), "pre-filter failed"
    syms_v1 = extract_iac_extended_symbols(cdk_v1_ts, "lib/stack.ts", ".ts")
    _check(
        "CDK v1 TS Stack",
        any(s["name"] == "MyV1Stack" and s["symbol_type"] == "use_case" and s.get("cdk_version") == "v1" for s in syms_v1),
        str(syms_v1),
    )

    # ── Test 2: CDK v2 TypeScript Stack + resource extraction ─────────────────
    cdk_v2_ts = """
import * as cdk from 'aws-cdk-lib'
import { aws_s3 as s3 } from 'aws-cdk-lib'
import { aws_lambda as lambda } from 'aws-cdk-lib'
import { Construct } from 'constructs'

export class ApiStack extends cdk.Stack {
  constructor(scope: Construct, id: string) {
    super(scope, id)
    const bucket = new s3.Bucket(this, 'DataBucket', { encryption: s3.BucketEncryption.S3_MANAGED })
    const fn = new lambda.Function(this, 'Handler', { runtime: lambda.Runtime.NODEJS_18_X, handler: 'index.handler', code: lambda.Code.fromAsset('lambda') })
  }
}
"""
    assert supports_iac_extended(cdk_v2_ts, "lib/api.ts", ".ts"), "v2 pre-filter failed"
    syms_v2 = extract_iac_extended_symbols(cdk_v2_ts, "lib/api.ts", ".ts")
    _check(
        "CDK v2 TS Stack",
        any(s["name"] == "ApiStack" and s["symbol_type"] == "use_case" and s.get("cdk_version") == "v2" for s in syms_v2),
        str([s["name"] for s in syms_v2]),
    )
    _check(
        "CDK v2 TS resource extraction (Bucket)",
        any("DataBucket" in s["name"] for s in syms_v2),
        str([s["name"] for s in syms_v2]),
    )
    edges_v2 = parse_iac_extended_imports(cdk_v2_ts, "lib/api.ts", ".ts")
    _check(
        "CDK v2 TS stack→resource edges",
        any(e["relation"] == "references" for e in edges_v2),
        str(edges_v2),
    )

    # ── Test 3: CDK Python Stack ──────────────────────────────────────────────
    cdk_py = """
from aws_cdk import Stack, aws_s3 as s3, aws_lambda as lambda_
from constructs import Construct

class BackendStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)
        bucket = s3.Bucket(self, "AppBucket", versioned=True)
        fn = lambda_.Function(self, "Processor", runtime=lambda_.Runtime.PYTHON_3_11, handler="app.handler", code=lambda_.Code.from_asset("src"))
"""
    assert supports_iac_extended(cdk_py, "infra/stack.py", ".py"), "CDK py pre-filter failed"
    syms_py = extract_iac_extended_symbols(cdk_py, "infra/stack.py", ".py")
    _check(
        "CDK Python Stack",
        any(s["name"] == "BackendStack" and s["symbol_type"] == "use_case" for s in syms_py),
        str([s["name"] for s in syms_py]),
    )
    _check(
        "CDK Python resource extraction",
        any("AppBucket" in s["name"] or "Bucket" in s.get("resource_type", "") for s in syms_py),
        str([s["name"] for s in syms_py]),
    )

    # ── Test 4: CloudFormation YAML resource extraction ───────────────────────
    cfn_yaml = """
AWSTemplateFormatVersion: '2010-09-09'
Description: Sample S3 + Lambda template

Parameters:
  BucketName:
    Type: String
    Default: my-bucket

Resources:
  DataBucket:
    Type: AWS::S3::Bucket
    Properties:
      VersioningConfiguration:
        Status: Enabled

  ProcessorFunction:
    Type: AWS::Lambda::Function
    DependsOn: DataBucket
    Properties:
      Handler: index.handler
      Runtime: nodejs18.x
      Code:
        S3Bucket: !Ref DataBucket

  NestedChild:
    Type: AWS::CloudFormation::Stack
    Properties:
      TemplateURL: https://s3.amazonaws.com/bucket/child.template

Outputs:
  BucketArn:
    Value: !GetAtt DataBucket.Arn
"""
    assert supports_iac_extended(cfn_yaml, "templates/main.yaml", ".yaml"), "CFN YAML pre-filter failed"
    syms_cfn = extract_iac_extended_symbols(cfn_yaml, "templates/main.yaml", ".yaml")
    _check(
        "CFN YAML resource extraction (S3::Bucket)",
        any(s["name"] == "DataBucket" and s.get("resource_type") == "AWS::S3::Bucket" for s in syms_cfn),
        str([(s["name"], s.get("resource_type")) for s in syms_cfn]),
    )
    _check(
        "CFN YAML parameter extraction",
        any(s["name"] == "BucketName" and s["symbol_type"] == "utility" for s in syms_cfn),
        str([s["name"] for s in syms_cfn]),
    )
    _check(
        "CFN YAML output extraction",
        any(s["name"] == "BucketArn" for s in syms_cfn),
        str([s["name"] for s in syms_cfn]),
    )
    _check(
        "CFN YAML nested stack is use_case",
        any(s["name"] == "NestedChild" and s["symbol_type"] == "use_case" for s in syms_cfn),
        str([(s["name"], s["symbol_type"]) for s in syms_cfn]),
    )
    edges_cfn = parse_iac_extended_imports(cfn_yaml, "templates/main.yaml", ".yaml")
    _check(
        "CFN YAML DependsOn edge",
        any(e["relation"] == "needs" and "DataBucket" in e["to_id"] for e in edges_cfn),
        str(edges_cfn),
    )

    # ── Test 5: CloudFormation JSON resource extraction ───────────────────────
    cfn_json = """{
  "AWSTemplateFormatVersion": "2010-09-09",
  "Parameters": {
    "Env": {
      "Type": "String",
      "Default": "prod"
    }
  },
  "Resources": {
    "AppTable": {
      "Type": "AWS::DynamoDB::Table",
      "Properties": {
        "TableName": "app-table",
        "BillingMode": "PAY_PER_REQUEST"
      }
    },
    "AppQueue": {
      "Type": "AWS::SQS::Queue",
      "DependsOn": ["AppTable"],
      "Properties": {
        "QueueName": "app-queue"
      }
    }
  },
  "Outputs": {
    "TableArn": {
      "Value": { "Fn::GetAtt": ["AppTable", "Arn"] }
    }
  }
}"""
    assert supports_iac_extended(cfn_json, "templates/app.json", ".json"), "CFN JSON pre-filter failed"
    syms_cfn_j = extract_iac_extended_symbols(cfn_json, "templates/app.json", ".json")
    _check(
        "CFN JSON resource extraction (DynamoDB)",
        any(s["name"] == "AppTable" and s.get("resource_type") == "AWS::DynamoDB::Table" for s in syms_cfn_j),
        str([(s["name"], s.get("resource_type")) for s in syms_cfn_j]),
    )
    _check(
        "CFN JSON parameter extraction",
        any(s["name"] == "Env" and s["symbol_type"] == "utility" for s in syms_cfn_j),
        str([s["name"] for s in syms_cfn_j]),
    )

    # ── Test 6: Pulumi TypeScript ComponentResource ───────────────────────────
    pulumi_ts = """
import * as pulumi from '@pulumi/pulumi'
import * as aws from '@pulumi/aws'

export class VpcNetwork extends pulumi.ComponentResource {
  public readonly vpcId: pulumi.Output<string>
  constructor(name: string, opts?: pulumi.ComponentResourceOptions) {
    super('myapp:network:VpcNetwork', name, {}, opts)
    const vpc = new aws.ec2.Vpc(`${name}-vpc`, { cidrBlock: '10.0.0.0/16' })
    this.vpcId = vpc.id
  }
}

const config = new pulumi.Config()
const stackRef = new pulumi.StackReference('org/infra/prod')
export const vpcId = stackRef.getOutput('vpcId')
"""
    assert supports_iac_extended(pulumi_ts, "infra/network.ts", ".ts"), "Pulumi TS pre-filter failed"
    syms_pu = extract_iac_extended_symbols(pulumi_ts, "infra/network.ts", ".ts")
    _check(
        "Pulumi TS ComponentResource",
        any(s["name"] == "VpcNetwork" and s["symbol_type"] == "use_case" for s in syms_pu),
        str([s["name"] for s in syms_pu]),
    )
    edges_pu = parse_iac_extended_imports(pulumi_ts, "infra/network.ts", ".ts")
    _check(
        "Pulumi TS StackReference edge",
        any(e["relation"] == "references" for e in edges_pu),
        str(edges_pu),
    )

    # ── Test 7: Pulumi Python resource ────────────────────────────────────────
    pulumi_py = """
import pulumi
import pulumi_aws as aws

config = pulumi.Config()
env = config.require("environment")

bucket = aws.s3.Bucket("app-data", versioning=aws.s3.BucketVersioningArgs(enabled=True))
queue = aws.sqs.Queue("app-queue", message_retention_seconds=86400)

stack_ref = pulumi.StackReference("org/infra/prod")
vpc_id = stack_ref.get_output("vpcId")

pulumi.export("bucketArn", bucket.arn)
pulumi.export("queueUrl", queue.url)
"""
    assert supports_iac_extended(pulumi_py, "infra/main.py", ".py"), "Pulumi PY pre-filter failed"
    syms_pu_py = extract_iac_extended_symbols(pulumi_py, "infra/main.py", ".py")
    _check(
        "Pulumi Python resource extraction",
        any("app-data" in s["name"] or "bucket" in s["name"].lower() for s in syms_pu_py),
        str([s["name"] for s in syms_pu_py]),
    )
    _check(
        "Pulumi Python export",
        any(s["name"] == "bucketArn" for s in syms_pu_py),
        str([s["name"] for s in syms_pu_py]),
    )

    # ── Test 8: Bicep resource + module ───────────────────────────────────────
    bicep_content = """
targetScope = 'subscription'

param location string = 'eastus'
param environment string

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: 'rg-${environment}'
  location: location
}

module storage './modules/storage.bicep' = {
  name: 'storageDeployment'
  scope: rg
  params: {
    location: location
  }
}

module appService './modules/app.bicep' = {
  name: 'appServiceDeployment'
  scope: rg
}

output rgId string = rg.id
"""
    assert supports_iac_extended(bicep_content, "main.bicep", ".bicep"), "Bicep pre-filter failed"
    syms_bi = extract_iac_extended_symbols(bicep_content, "main.bicep", ".bicep")
    _check(
        "Bicep resource extraction",
        any(s["name"] == "rg" and s["symbol_type"] == "model" for s in syms_bi),
        str([(s["name"], s["symbol_type"]) for s in syms_bi]),
    )
    _check(
        "Bicep module extraction",
        any(s["name"] == "storage" and s["symbol_type"] == "utility" for s in syms_bi),
        str([(s["name"], s["symbol_type"]) for s in syms_bi]),
    )
    _check(
        "Bicep param extraction",
        any(s["name"] == "location" and "param" in s.get("resource_type", "").lower() for s in syms_bi),
        str([(s["name"], s.get("resource_type")) for s in syms_bi]),
    )
    _check(
        "Bicep output extraction",
        any(s["name"] == "rgId" for s in syms_bi),
        str([s["name"] for s in syms_bi]),
    )
    edges_bi = parse_iac_extended_imports(bicep_content, "main.bicep", ".bicep")
    _check(
        "Bicep module→parent contains edge",
        any(e["relation"] == "contains" for e in edges_bi),
        str(edges_bi),
    )

    # ── Test 9: ARM JSON resource ─────────────────────────────────────────────
    arm_json = """{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "storageAccountName": {
      "type": "string"
    }
  },
  "resources": [
    {
      "type": "Microsoft.Storage/storageAccounts",
      "apiVersion": "2021-04-01",
      "name": "[parameters('storageAccountName')]",
      "location": "[resourceGroup().location]",
      "kind": "StorageV2",
      "sku": {
        "name": "Standard_LRS"
      },
      "dependsOn": []
    },
    {
      "type": "Microsoft.Web/serverfarms",
      "apiVersion": "2021-02-01",
      "name": "myAppServicePlan",
      "location": "[resourceGroup().location]",
      "sku": {
        "name": "P1v2"
      },
      "dependsOn": ["Microsoft.Storage/storageAccounts/myStorage"]
    }
  ]
}"""
    assert supports_iac_extended(arm_json, "azuredeploy.json", ".json"), "ARM JSON pre-filter failed"
    syms_arm = extract_iac_extended_symbols(arm_json, "azuredeploy.json", ".json")
    _check(
        "ARM JSON resource extraction (Storage)",
        any("Storage" in s.get("resource_type", "") or "storageAccounts" in s["name"].lower() for s in syms_arm),
        str([(s["name"], s.get("resource_type")) for s in syms_arm]),
    )
    _check(
        "ARM JSON parameter extraction",
        any("storageAccountName" in s["name"] for s in syms_arm),
        str([s["name"] for s in syms_arm]),
    )

    print("\nALL IAC EXTENDED TESTS PASSED")

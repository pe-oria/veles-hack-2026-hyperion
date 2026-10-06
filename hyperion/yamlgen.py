"""HyperAI application descriptors: parameters -> YAML templates, plus LLM edit/repair passes.

New files are rendered by Python from a handful of parameters the LLM extracts, so they are
valid by construction. Only edits and repairs let the LLM write YAML, and the IDE validator
judges the result.
"""

import json
import re
from dataclasses import dataclass, field

import yaml

from hyperion import llm, prompts

# image -> (default port, entry point, args)
KNOWN_IMAGES: dict[str, tuple[int, str, list[str]]] = {
    "nginx": (80, "nginx", ["-g", "daemon off;"]),
    "httpd": (80, "httpd-foreground", []),
    "redis": (6379, "redis-server", []),
    "postgres": (5432, "postgres", []),
    "mysql": (3306, "mysqld", []),
    "mongo": (27017, "mongod", []),
    "hello-world": (80, "/hello", []),
}
WORKLOAD_KINDS = {"dockerimage": "DockerImage", "androidapk": "AndroidApk", "esp32binary": "esp32Binary"}
CHIPS = ("esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2")
PHASES = ("development", "testing", "production")
PLACEHOLDER_APK = "https://example.com/app.apk"
PLACEHOLDER_BINARY = "https://example.com/firmware.bin"

_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*(Mi|Gi|Ti|M|G|T)i?B?$", re.IGNORECASE)
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$", re.IGNORECASE)


@dataclass
class AppParams:
    kind: str = "native"  # native | device
    name: str = "my-app"
    description: str = ""
    owner: str = "my-team"
    lifecycle_phase: str = "development"
    image: str = "nginx"
    tag: str = "latest"
    port: int = 80
    cpu: str | None = None  # millicores, e.g. "500m"
    memory: str | None = None  # e.g. "1Gi"
    storage: str | None = None
    workload_kind: str = "DockerImage"
    apk_url: str = PLACEHOLDER_APK
    package_name: str = "com.example.app"
    binary_url: str = PLACEHOLDER_BINARY
    chip: str = "esp32"
    device_name: str | None = None
    placeholders: list[str] = field(default_factory=list)  # values the user still has to replace


def slug(value: object, default: str = "") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return cleaned[:40].strip("-") or default


def clean_cpu(value: object) -> str | None:
    """Accept "500m", or a number of cores such as 2 / "0.5"; return millicores or None."""
    text = str(value or "").strip().lower().removesuffix("cores").removesuffix("core").strip()
    if re.fullmatch(r"[1-9]\d*m", text):
        return text
    if re.fullmatch(r"\d+(\.\d+)?", text) and 0 < float(text) <= 256:
        return f"{round(float(text) * 1000)}m"
    return None


def clean_size(value: object) -> str | None:
    """Accept "2Gi", "512Mi", "2GB", "512 MB"; return a Kubernetes-style size or None."""
    match = _SIZE.match(str(value or "").strip())
    if not match:
        return None
    return f"{match.group(1)}{match.group(2)[0].upper()}i"


def split_image(value: object) -> tuple[str, str | None]:
    """"nginx:1.25" -> ("nginx", "1.25"); a registry port is not mistaken for a tag."""
    image = str(value or "").strip()
    head, sep, tail = image.rpartition(":")
    if sep and head and "/" not in tail:
        return head, tail
    return image, None


def clean_params(data: dict, kind: str | None = None, image_hint: str | None = None) -> AppParams:
    """Turn the LLM's loosely-typed JSON into safe parameters, falling back to defaults."""
    params = AppParams(kind="device" if (kind or data.get("kind")) == "device" else "native")

    workload = WORKLOAD_KINDS.get(str(data.get("workload_kind") or "").lower().replace("_", ""))
    if params.kind == "device" and workload:
        params.workload_kind = workload

    image, tag = split_image(data.get("image") or image_hint)
    if image:
        params.image = image
    params.tag = str(data.get("tag") or tag or "latest").strip()
    base = params.image.rsplit("/", 1)[-1]

    has_image = bool(image) and params.workload_kind == "DockerImage"
    if not image and params.workload_kind == "DockerImage":
        label = "spec.workload.dockerImage.image" if params.kind == "device" else "specs.runtime.containerImage"
        params.placeholders.append(f"`{label}` ({params.image})")
    params.name = slug(data.get("name"), slug(base, "my-app") if has_image else "my-app")
    params.description = str(data.get("description") or "").strip()[:200]
    params.owner = str(data.get("owner") or params.owner).strip()[:60]
    phase = str(data.get("lifecycle_phase") or "").lower()
    params.lifecycle_phase = phase if phase in PHASES else ("development" if params.kind == "native" else "testing")

    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        port = 0
    params.port = port if 1 <= port <= 65535 else KNOWN_IMAGES.get(base, (80,))[0]

    params.cpu = clean_cpu(data.get("cpu"))
    params.memory = clean_size(data.get("memory"))
    params.storage = clean_size(data.get("storage"))

    for key, placeholder, label in (
        ("apk_url", PLACEHOLDER_APK, "workload.androidApk.apkUrl"),
        ("binary_url", PLACEHOLDER_BINARY, "workload.esp32Binary.binaryUrl"),
    ):
        value = str(data.get(key) or "").strip()
        needed = params.workload_kind == ("AndroidApk" if key == "apk_url" else "esp32Binary")
        if _URL.match(value):
            setattr(params, key, value)
        elif needed:
            params.placeholders.append(f"`{label}` ({placeholder})")
    package = str(data.get("package_name") or "").strip()
    if re.fullmatch(r"[A-Za-z][\w]*(\.[A-Za-z][\w]*)+", package):
        params.package_name = package
    elif params.workload_kind == "AndroidApk":
        params.placeholders.append(f"`workload.androidApk.packageName` ({params.package_name})")
    chip = str(data.get("chip") or "").lower()
    params.chip = chip if chip in CHIPS else "esp32"
    params.device_name = str(data.get("device_name") or "").strip() or None
    return params


def native_profile(p: AppParams) -> dict:
    base = p.image.rsplit("/", 1)[-1]
    _, entry_point, args = KNOWN_IMAGES.get(base, (0, base, []))
    return {
        "applicationProfile": {
            "metadata": {
                "type": "native",
                "schemaVersion": "1.1.0",
                "name": p.name,
                "version": "1.0.0",
                "description": p.description or f"{base} service on the HyperAI platform.",
                "owner": p.owner,
                "lifecyclePhase": p.lifecycle_phase,
            },
            "specs": {
                "runtime": {
                    "executionType": "container",
                    "entryPoint": entry_point,
                    "args": list(args),
                    "baseOS": {"name": "linux", "version": "latest"},
                    "containerImage": {"uri": p.image, "tag": p.tag},
                },
                "resources": {"cpu": p.cpu or "1000m", "memory": p.memory or "1Gi", "storage": p.storage or "1Gi"},
                "network": {"ports": [{"port": p.port, "protocol": "TCP", "publicExposure": True}]},
                "constraints": {"supportedArchitectures": ["x86_64", "arm64"], "isHighlyAvailable": False},
                "qos": {"startupTime": "10s", "availability": "99.0%"},
            }
        }
    }


def device_manifest(p: AppParams) -> dict:
    if p.workload_kind == "AndroidApk":
        workload = {"androidApk": {"apkUrl": p.apk_url, "packageName": p.package_name, "installMode": "install"}}
        architectures = ["arm64-v8a"]
    elif p.workload_kind == "esp32Binary":
        workload = {"esp32Binary": {"binaryUrl": p.binary_url, "chip": p.chip, "flash": {"method": "ota"}}}
        architectures = ["xtensa"]
    else:
        image = p.image if p.tag == "latest" else f"{p.image}:{p.tag}"
        workload = {"dockerImage": {"image": image, "imagePullPolicy": "IfNotPresent"}}
        architectures = ["amd64", "arm64"]

    spec: dict = {}
    if p.device_name:
        spec["device_name"] = p.device_name
    spec["app"] = {
        "type": "device",
        "schemaVersion": "1.0.0",
        "name": p.name,
        "version": "1.0.0",
        "description": p.description or f"{p.name} device application.",
        "owner": p.owner,
        "lifecyclePhase": p.lifecycle_phase,
    }
    spec["workload"] = {"kind": p.workload_kind, **workload}
    resources = {}
    if p.cpu:
        resources["cpu"] = {"value": int(p.cpu[:-1]), "unit": "millicores"}
    if p.memory:
        resources["memory"] = {"value": float(p.memory[:-2]), "unit": f"{p.memory[-2:]}B"}
    if resources:
        spec["resources"] = resources
    spec["network"] = {
        "ports": [{"port": p.port, "protocol": "HTTP"}],
        "networkBandwidthMin": {"value": 1, "unit": "Mbps"},
    }
    spec["qos"] = {
        "latencyToleranceMax": {"value": 500, "unit": "ms"},
        "energyCost": {"value": 1, "unit": "W"},
        "monetaryCost": {"value": 0.01, "currency": "USD", "per": "hour"},
        "resilience": "auto-restart",
        "availability": {"value": 0.9, "unit": "fraction"},
        "startupTime": {"value": 5, "unit": "s"},
    }
    spec["constraints"] = {
        "schedulingPriority": 1,
        "supportedArchitectures": architectures,
        "geoLocationRequirement": "LocalZone",
        "isHighlyAvailable": False,
        "faultTolerance": "graceful-degradation",
        "dataClassification": "internal",
    }
    return {"apiVersion": "hyper.ai/v1", "kind": "Application", "metadata": {"name": p.name}, "spec": spec}


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid YAML string


def _emit(node: object, indent: int = 0) -> list[str]:
    """Cookbook-style YAML: quoted string values, lists of scalars inline."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, dict) and value:
                lines += [f"{pad}{key}:", *_emit(value, indent + 1)]
            elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                lines.append(f"{pad}{key}:")
                for item in value:
                    first, *rest = _emit(item, indent + 2)
                    lines += [f"{pad}  - {first.strip()}", *rest]
            elif isinstance(value, (list, dict)):
                inline = ", ".join(_scalar(item) for item in value) if isinstance(value, list) else ""
                lines.append(f"{pad}{key}: [{inline}]" if isinstance(value, list) else f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}: {_scalar(value)}")
    return lines


def render(params: AppParams) -> str:
    document = device_manifest(params) if params.kind == "device" else native_profile(params)
    return "\n".join(_emit(document)) + "\n"


def detect_kind(content: str) -> str | None:
    """native | device | None, the same way the IDE validator tells profiles apart."""
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    if "applicationProfile" in data:
        return "native"
    if "apiVersion" in data or "kind" in data:
        return "device"
    return None


def strip_fences(reply: str) -> str:
    """The file content from an LLM reply that may wrap it in a code fence or add chatter."""
    fenced = re.search(r"```[\w-]*\n(.*?)```", reply, re.DOTALL)
    return (fenced.group(1) if fenced else reply).strip("\n") + "\n"


_NUMBER_WORDS = re.compile(r"\b(half|quarter|one|two|three|four|six|eight)\b")
_LITERAL_FIELDS = ("tag", "port", "owner", "device_name", "apk_url", "binary_url", "package_name")


def ground(data: dict, text: str) -> dict:
    """Drop extracted values the user never wrote: the model copies them from its examples."""
    lowered = text.lower()
    compact = re.sub(r"[\s_-]", "", lowered)
    kept = dict(data)
    for key in _LITERAL_FIELDS:
        if kept.get(key) is not None and str(kept[key]).lower() not in lowered:
            kept[key] = None
    for key in ("cpu", "memory", "storage"):
        digits = re.search(r"\d+(\.\d+)?", str(kept.get(key) or ""))
        stated = digits and re.search(rf"(?<![\d.]){re.escape(digits.group())}(?!\d)", lowered)
        if kept.get(key) is not None and not stated and not (key == "cpu" and _NUMBER_WORDS.search(lowered)):
            kept[key] = None
    if kept.get("chip") and str(kept["chip"]).lower() not in compact:
        kept["chip"] = None
    if kept.get("lifecycle_phase") and str(kept["lifecycle_phase"]).lower()[:4] not in lowered:
        kept["lifecycle_phase"] = None
    image = split_image(kept.get("image"))[0].rsplit("/", 1)[-1].lower()
    if image and image not in lowered:
        kept["image"] = None
    return kept


_SIZE_TEXT = r"(\d+(?:\.\d+)?\s*(?:Gi|Mi|Ti|GB|MB|TB|G|M)B?)"
_STATED = {
    "memory": [rf"{_SIZE_TEXT}\s*(?:of\s+)?(?:memory|ram)\b", rf"\b(?:memory|ram)\b\D{{0,12}}{_SIZE_TEXT}"],
    "storage": [rf"{_SIZE_TEXT}\s*(?:of\s+)?(?:storage|disk)\b", rf"\b(?:storage|disk)\b\D{{0,12}}{_SIZE_TEXT}"],
    "cpu": [r"(\d+(?:\.\d+)?)\s*(?:v?cpus?|cores?)\b", r"\b(\d+m)\b", r"\bcpu\b\D{0,12}(\d+(?:\.\d+)?m?)\b"],
    "port": [r"\bport\s+(\d{2,5})\b", r"\b(?:listening|listens|exposed?)\s+on\s+(\d{2,5})\b"],
}


def stated_params(text: str) -> dict:
    """Resource values and ports read straight from the user's words, no LLM involved."""
    found = {}
    for key, patterns in _STATED.items():
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                found[key] = match.group(1)
                break
    return found


async def extract_params(text: str, kind: str | None, image_hint: str | None) -> AppParams:
    """One JSON-mode call: the user's request -> template parameters."""
    messages = [("system", prompts.PARAMS_SYSTEM)]
    for example, answer in prompts.PARAMS_EXAMPLES:
        messages += [("human", example), ("ai", answer)]
    messages.append(("human", text[:1000]))
    extracted = ground(await llm.ask_json(messages), text)
    # the model sometimes answers with a whole manifest instead: fixed-format values have a net
    data = {**extracted, **{key: value for key, value in stated_params(text).items() if not extracted.get(key)}}
    if image_hint and split_image(image_hint)[0].rsplit("/", 1)[-1].lower() not in text.lower():
        image_hint = None
    return clean_params(data, kind, image_hint)


async def _rewrite(system: str, user: str) -> str:
    reply = await llm.edit_llm.ainvoke([("system", system), ("human", user)])
    return strip_fences(reply.text)


def rules_for(kind: str | None) -> str:
    return {"native": prompts.NATIVE_RULES, "device": prompts.DEVICE_RULES}.get(kind or "", prompts.GENERIC_RULES)


async def edit(content: str, instruction: str) -> str:
    """Return the whole file with the requested change applied."""
    system = prompts.EDIT_SYSTEM.format(rules=rules_for(detect_kind(content)))
    return await _rewrite(system, prompts.EDIT_USER.format(content=content, instruction=instruction))


async def repair(content: str, errors: list[dict]) -> str:
    """Return the whole file with the validator's errors fixed."""
    listed = "\n".join(f"- line {e.get('line')}: {e.get('field')} {e.get('message')}" for e in errors[:12])
    system = prompts.EDIT_SYSTEM.format(rules=rules_for(detect_kind(content)))
    return await _rewrite(system, prompts.REPAIR_USER.format(content=content, errors=listed))


async def write_plain(path: str, text: str) -> str:
    """Content for a file that is not an application descriptor (README, notes, ...)."""
    reply = await llm.edit_llm.ainvoke(
        [("system", prompts.PLAIN_FILE_SYSTEM), ("human", prompts.PLAIN_FILE_USER.format(path=path, text=text))]
    )
    return strip_fences(reply.text)

import yaml

from hyperion import yamlgen
from hyperion.yamlgen import clean_cpu, clean_params, clean_size, render, split_image


def test_units_are_normalised_to_what_the_validator_accepts():
    assert [clean_cpu(v) for v in ("500m", "2", 0.5, "2 cores", "0", "lots", None)] == [
        "500m", "2000m", "500m", "2000m", None, None, None]
    assert [clean_size(v) for v in ("2Gi", "512Mi", "2GB", "512 MB", "1g", "big", None)] == [
        "2Gi", "512Mi", "2Gi", "512Mi", "1Gi", None, None]


def test_split_image():
    assert split_image("nginx") == ("nginx", None)
    assert split_image("redis:7") == ("redis", "7")
    assert split_image("localhost:5000/team/api") == ("localhost:5000/team/api", None)
    assert split_image("ghcr.io/acme/api:2.1") == ("ghcr.io/acme/api", "2.1")


def test_native_defaults_come_from_the_image():
    params = clean_params({}, "native", "redis:7")
    profile = yaml.safe_load(render(params))["applicationProfile"]
    assert profile["metadata"]["name"] == "redis" and profile["metadata"]["type"] == "native"
    assert profile["specs"]["runtime"]["containerImage"] == {"uri": "redis", "tag": "7"}
    assert profile["specs"]["network"]["ports"][0]["port"] == 6379
    assert profile["specs"]["resources"] == {"cpu": "1000m", "memory": "1Gi", "storage": "1Gi"}


def test_bad_llm_values_fall_back_to_defaults():
    params = clean_params({"port": "eighty", "cpu": "fast", "memory": "huge", "lifecycle_phase": "beta",
                           "name": "My App!!", "tag": 7}, "native", "nginx")
    assert (params.port, params.cpu, params.memory, params.lifecycle_phase) == (80, None, None, "development")
    assert params.name == "my-app" and params.tag == "7"


def test_rendered_values_keep_their_types():
    text = render(clean_params({"tag": "7", "description": 'Say "hi": yes # not a comment'}, "native", "redis"))
    profile = yaml.safe_load(text)["applicationProfile"]
    assert profile["specs"]["runtime"]["containerImage"]["tag"] == "7"  # a string, not the number 7
    assert profile["metadata"]["version"] == "1.0.0"
    assert profile["metadata"]["description"] == 'Say "hi": yes # not a comment'
    assert profile["specs"]["runtime"]["args"] == []
    assert profile["specs"]["constraints"]["isHighlyAvailable"] is False


def test_device_workloads_have_exactly_one_matching_block():
    blocks = {"DockerImage": "dockerImage", "AndroidApk": "androidApk", "esp32Binary": "esp32Binary"}
    for kind, block in blocks.items():
        params = clean_params({"workload_kind": kind}, "device", "hello-world")
        manifest = yaml.safe_load(render(params))
        workload = manifest["spec"]["workload"]
        assert manifest["apiVersion"] == "hyper.ai/v1" and manifest["kind"] == "Application"
        assert set(workload) == {"kind", block} and workload["kind"] == kind
        assert set(manifest["spec"]["qos"]) == {
            "latencyToleranceMax", "energyCost", "monetaryCost", "resilience", "availability", "startupTime"}


def test_missing_urls_become_reported_placeholders():
    android = clean_params({"workload_kind": "AndroidApk"}, "device")
    assert len(android.placeholders) == 2 and android.apk_url == yamlgen.PLACEHOLDER_APK
    given = clean_params({"workload_kind": "AndroidApk", "apk_url": "https://acme.io/cam.apk",
                          "package_name": "com.acme.cam"}, "device")
    assert given.placeholders == [] and given.apk_url == "https://acme.io/cam.apk"
    assert clean_params({}, "native", "redis").placeholders == []
    # no image named: nginx is only a stand-in, and the user is told
    assert clean_params({}, "native").placeholders == ["specs.runtime.containerImage (nginx)"]


def test_device_resources_use_value_unit_objects():
    params = clean_params({"cpu": "500m", "memory": "2Gi"}, "device", "nginx")
    resources = yaml.safe_load(render(params))["spec"]["resources"]
    assert resources == {"cpu": {"value": 500, "unit": "millicores"}, "memory": {"value": 2.0, "unit": "GiB"}}


def test_detect_kind_and_strip_fences():
    assert yamlgen.detect_kind("applicationProfile: {}") == "native"
    assert yamlgen.detect_kind("apiVersion: hyper.ai/v1\nkind: Application") == "device"
    assert yamlgen.detect_kind("name: x") is None and yamlgen.detect_kind("a: [") is None
    assert yamlgen.strip_fences("Here you go:\n```yaml\na: 1\nb: 2\n```\nDone.") == "a: 1\nb: 2\n"
    assert yamlgen.strip_fences("a: 1") == "a: 1\n"


def test_ground_drops_values_the_user_never_wrote():
    copied = {"image": "redis", "tag": "7", "port": 6379, "memory": "512Mi", "cpu": "0.5", "owner": "team Kestrel",
              "lifecycle_phase": "production", "chip": "esp32s3", "apk_url": "https://acme.io/cam.apk"}
    kept = yamlgen.ground(copied, "put a redis profile in the demo folder")
    assert kept["image"] == "redis"
    assert all(kept[key] is None for key in copied if key != "image")


def test_ground_keeps_what_the_user_stated():
    text = ("production profile for redis:7 with 512Mi of memory and half a core on port 6379, owned by team Kestrel, "
            "plus an ESP32-S3 note and https://acme.io/cam.apk")
    stated = {"image": "redis", "tag": "7", "port": 6379, "memory": "512Mi", "cpu": "0.5", "owner": "team Kestrel",
              "lifecycle_phase": "production", "chip": "esp32s3", "apk_url": "https://acme.io/cam.apk"}
    assert yamlgen.ground(stated, text) == stated
    # "7" must be stated as a number of its own, not found inside another one
    assert yamlgen.ground({"memory": "7Gi"}, "nginx on port 8070")["memory"] is None
    assert yamlgen.ground({"image": "postgres"}, "a database service")["image"] is None


def test_stated_params_reads_fixed_format_values_from_the_text():
    stated = yamlgen.stated_params
    assert stated("nginx:1.27 Docker image with 4Gi of memory") == {"memory": "4Gi"}
    assert stated("2 cpus, 4Gi memory and 20Gi storage on port 8080") == {
        "cpu": "2", "memory": "4Gi", "storage": "20Gi", "port": "8080"}
    assert stated("postgres 16 with 2GB RAM listening on 5433, cpu 750m") == {
        "memory": "2GB", "port": "5433", "cpu": "750m"}
    assert stated("set the memory to 512 MB") == {"memory": "512 MB"}
    assert stated("Create a deployment YAML for a service using the nginx Docker image") == {}
    assert stated("a redis:7 cache") == {}


async def test_extraction_survives_a_model_that_writes_a_manifest_instead(monkeypatch):
    async def manifest_instead_of_params(messages):
        return {"apiVersion": "apps/v1", "kind": "Deployment", "spec": {"replicas": 1}}

    monkeypatch.setattr(yamlgen.llm, "ask_json", manifest_instead_of_params)
    text = "Create a deployment YAML for a service using the nginx:1.27 Docker image with 4Gi of memory on port 8080"
    params = await yamlgen.extract_params(text, "native", "nginx:1.27")
    assert (params.kind, params.image, params.tag, params.memory, params.port) == ("native", "nginx", "1.27", "4Gi", 8080)


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "text, image",
    [
        ("Make a postgres application profile with 2Gi of memory", "postgres"),
        ("I need a descriptor for a mongo database listening on 27017", "mongo"),
        ("generate a manifest for an httpd web server with 500m cpu", "httpd"),
        ("a profile for a PostgreSQL database", "postgres"),
        ("deploy mongodb please", "mongo"),
        ("an apache web server profile", "httpd"),
        ("Create a deployment YAML for a service using the nginx Docker image", "nginx"),
        ("write an app profile for myuser/app:1.2 and save it as services/app.yaml", "myuser/app:1.2"),
        ("a profile for ghcr.io/acme/api listening on 8080", "ghcr.io/acme/api"),
        ("profile for localhost:5000/team/api:2", "localhost:5000/team/api:2"),
        ("a deployment for the kafka/broker image", "kafka/broker"),
        ("deployment yaml for redis:7.2.", "redis:7.2"),
        ("a deployment file for mariadb 11", "mariadb:11"),
        ("run the eclipse-mosquitto image on node rpi-7", "eclipse-mosquitto"),
        ("device app running the hello-world Docker image", "hello-world"),
        ("a nodejs api", "node"),
    ],
)
def test_guess_image_reads_explicit_and_well_known_names(text, image):
    assert yamlgen.guess_image(text) == (image, True)


@pytest.mark.parametrize(
    "text",
    [
        "create a deployment yaml for my web service",
        "put the profile in demo/redis.yaml",  # a path, and redis.yaml is a file name
        "the apk is at https://acme.io/cam.apk",
        "register it on node rpi-7",
        "What is a DeviceNode?",
        "save it under services/api",
        "expose it on port:8080",
        "a descriptor for a web server",
    ],
)
def test_guess_image_does_not_invent(text):
    assert yamlgen.guess_image(text) is None


def test_loose_wording_is_a_reported_guess():
    assert yamlgen.guess_image("a profile for a clickhouse database") == ("clickhouse", False)
    params = yamlgen.build_params({}, "a profile for a clickhouse database", "native", None)
    assert params.image == "clickhouse" and params.image_guessed and not params.image_missing
    assert any("my reading of your request" in item for item in params.placeholders)


def test_build_params_names_the_file_after_the_image_and_never_falls_back_to_nginx():
    params = yamlgen.build_params({"name": "pg-main"}, "Make a postgres application profile with 2Gi of memory", "native", None)
    assert (params.image, params.memory, params.port, params.name, params.file_stem) == (
        "postgres", "2Gi", 5432, "pg-main", "postgres")
    assert not params.image_missing and params.placeholders == []

    missing = yamlgen.build_params({}, "create a deployment yaml for my web service", "native", None)
    assert missing.image_missing


def test_explicit_reference_beats_the_models_reading():
    params = yamlgen.build_params({"image": "billing", "tag": "latest"},
                                  "an application profile for the acme/billing:3.0 container on port 9000", None, "billing")
    assert (params.image, params.tag, params.file_stem, params.port) == ("acme/billing", "3.0", "billing", 9000)


def test_workload_is_read_from_the_text_even_if_the_model_misses_it():
    esp = yamlgen.build_params({}, "write a descriptor for an ESP32-S3 temperature sensor firmware", None, None)
    assert (esp.kind, esp.workload_kind, esp.image_missing) == ("device", "esp32Binary", False)
    apk = yamlgen.build_params({}, "manifest for our Android app", "native", None)
    assert (apk.kind, apk.workload_kind) == ("device", "AndroidApk")


@pytest.mark.parametrize(
    "reply, image",
    [("postgres:16", "postgres:16"), ("use redis", "redis"), ("the nginx image please", "nginx"),
     ("myuser/app:1.2", "myuser/app:1.2"), ("clickhouse", "clickhouse"), ("it's ghcr.io/acme/api", "ghcr.io/acme/api")],
)
def test_image_from_reply_accepts_short_answers(reply, image):
    assert yamlgen.image_from_reply(reply) == image


@pytest.mark.parametrize("reply", ["What is HyperAI?", "cancel", "no", "never mind, tell me about open connectors instead",
                                   "I do not know yet", ""])
def test_image_from_reply_ignores_everything_else(reply):
    assert yamlgen.image_from_reply(reply) is None

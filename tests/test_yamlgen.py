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
    assert clean_params({}, "native").placeholders == ["`specs.runtime.containerImage` (nginx)"]


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

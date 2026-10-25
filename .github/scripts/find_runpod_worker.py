import json
import os
import re
import urllib.error
import urllib.request

api_key = os.environ["RUNPOD_API_KEY"]
prefix = os.environ.get("POD_NAME_PREFIX", "dameokja-ai-worker")
template_id = os.environ.get("TEMPLATE_ID", "")
worker_port = int(os.environ.get("WORKER_PORT", "9000"))
pattern = re.compile(rf"^{re.escape(prefix)}-v(\d+)$")
migration_pattern = re.compile(rf"^{re.escape(prefix)}-v(\d+)-migration$")
request = urllib.request.Request(
    "https://api.runpod.io/v2/pods?limit=1000",
    headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "ktb-runpod-deploy/1.0",
    },
)

try:
    with urllib.request.urlopen(request, timeout=30) as response:
        pods = json.load(response).get("pods", [])
except (urllib.error.URLError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Runpod Pod 목록을 가져오지 못했습니다: {exc}") from exc

candidates = []
migrations = []
for pod in pods:
    if template_id and pod.get("template") != template_id:
        continue
    # 마이그레이션 중에는 정식 이름의 이전 EXITED Pod에만 최신 이미지를 배포하면 안 된다.
    # 대상의 데이터 이전·준비 검증과 이름 승격을 마친 뒤 같은 배포를 다시 실행한다.
    if migration_pattern.fullmatch(str(pod.get("name", ""))) and pod.get("id"):
        migrations.append(str(pod["id"]))
    match = pattern.fullmatch(str(pod.get("name", "")))
    if not match or pod.get("status") not in {"RUNNING", "EXITED"}:
        continue
    if pod.get("id"):
        candidates.append((int(match.group(1)), str(pod.get("startedAt") or ""), str(pod["id"])))

if migrations:
    raise SystemExit("마이그레이션 대상이 남아 있어 이전 Pod 배포를 차단합니다. 이전 완료·이름 승격 후 배포를 다시 실행하세요: " + ", ".join(sorted(migrations)))

if not candidates:
    raise SystemExit(f"배포 가능한 {prefix}-vN Worker를 찾지 못했습니다.")

candidates.sort(reverse=True)
highest_version = candidates[0][0]
highest = [item for item in candidates if item[0] == highest_version]
if len(highest) != 1:
    ids = ", ".join(item[2] for item in highest)
    raise SystemExit(f"v{highest_version} Worker가 여러 개라 배포 대상을 결정할 수 없습니다: {ids}")

pod_id = highest[0][2]
pod_status = next(
    str(pod.get("status")) for pod in pods if str(pod.get("id")) == pod_id
)
health_url = f"https://{pod_id}-{worker_port}.proxy.runpod.net/health/ready"
if pod_status == "RUNNING":
    health_request = urllib.request.Request(
        health_url,
        headers={"Accept": "application/json", "User-Agent": "ktb-runpod-deploy/1.0"},
    )
    try:
        with urllib.request.urlopen(health_request, timeout=30) as response:
            ready = bool(json.load(response).get("ready"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"선택한 Worker의 준비 상태를 확인하지 못했습니다: {exc}") from exc
    if not ready:
        raise SystemExit(f"선택한 v{highest_version} Worker가 아직 준비되지 않았습니다: {pod_id}")

print(f"POD_ID={pod_id}")
print(f"POD_STATUS={pod_status}")
print(f"WORKER_PORT={worker_port}")

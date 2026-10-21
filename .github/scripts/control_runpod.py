import json
import os
import re
import sys
import time
import urllib.error
import urllib.request


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"RunPod API {status}: {message}")
        self.status = status


API_KEY = os.environ["RUNPOD_API_KEY"]
ACTION = os.environ["ACTION"]
PREFIX = os.environ.get("RUNPOD_POD_NAME_PREFIX", "dameokja-ai-worker")
TEMPLATE_ID = os.environ.get("RUNPOD_TEMPLATE_ID", "")
WORKER_PORT = int(os.environ.get("RUNPOD_WORKER_PORT", "9000"))
HEALTH_PATH = os.environ.get("RUNPOD_HEALTH_PATH", "/health/ready")
POLL_SECONDS = int(os.environ.get("RUNPOD_POLL_SECONDS", "10"))
START_TIMEOUT = int(os.environ.get("RUNPOD_START_TIMEOUT_SECONDS", "3600"))
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "ktb-runpod-scheduler/1.0",
}
NAME_PATTERN = re.compile(rf"^{re.escape(PREFIX)}-v([0-9]+)$")


def request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://api.runpod.io{path}", data=data, headers=HEADERS, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace") or str(exc.reason)
        raise ApiError(exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise ApiError(0, str(exc.reason)) from exc


def graphql(query: str, variables: dict) -> dict:
    payload = request("POST", "/graphql", {"query": query, "variables": variables})
    errors = payload.get("errors") or []
    if errors:
        raise ApiError(400, errors[0].get("message", "GraphQL request failed"))
    return payload.get("data") or {}


def list_managed_pods() -> list[tuple[int, dict]]:
    pods = request("GET", "/v2/pods?limit=1000").get("pods", [])
    managed = []
    for pod in pods:
        match = NAME_PATTERN.fullmatch(str(pod.get("name", "")))
        if not match or not pod.get("id"):
            continue
        if TEMPLATE_ID and pod.get("template") != TEMPLATE_ID:
            continue
        managed.append((int(match.group(1)), pod))
    managed.sort(key=lambda item: (item[0], str(item[1].get("createdAt") or "")), reverse=True)
    return managed


def current_pod() -> tuple[int, dict]:
    managed = list_managed_pods()
    if not managed:
        raise RuntimeError(f"{PREFIX}-vN Pod를 찾지 못했습니다")
    highest_version = managed[0][0]
    same_version = [item for item in managed if item[0] == highest_version]
    if len(same_version) != 1:
        ids = ", ".join(str(item[1]["id"]) for item in same_version)
        raise RuntimeError(f"v{highest_version} Pod가 여러 개입니다: {ids}")
    return same_version[0]


def get_pod(pod_id: str) -> dict:
    return request("GET", f"/v2/pods/{pod_id}")


def pod_action(pod_id: str, action: str):
    return request("POST", f"/v2/pods/{pod_id}/action", {"action": action})


def wait_status(pod_id: str, wanted: set[str], timeout: int = START_TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    last_status = "UNKNOWN"
    while time.monotonic() < deadline:
        pod = get_pod(pod_id)
        last_status = str(pod.get("status", "UNKNOWN"))
        if last_status in wanted:
            return pod
        if last_status in {"ERROR", "TERMINATED"}:
            raise RuntimeError(f"Pod {pod_id} 상태가 {last_status}입니다")
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"Pod {pod_id} 상태 대기 시간 초과: {last_status}")


def wait_ready(pod_id: str) -> None:
    url = f"https://{pod_id}-{WORKER_PORT}.proxy.runpod.net{HEALTH_PATH}"
    deadline = time.monotonic() + START_TIMEOUT
    last_error = "응답 없음"
    while time.monotonic() < deadline:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": HEADERS["User-Agent"]}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.load(response)
                if response.status == 200 and payload.get("ready") is True:
                    print(f"Worker 준비 완료: {url}")
                    return
                last_error = f"ready={payload.get('ready')}"
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"Worker 준비 시간 초과: {last_error}")


def unlock(pod_id: str) -> None:
    query = """mutation podUnlock($input: PodLockInput!) {
      podUnlock(input: $input) { id locked }
    }"""
    try:
        graphql(query, {"input": {"podId": pod_id}})
    except ApiError as exc:
        print(f"Pod 잠금 해제 경고: {exc}", file=sys.stderr)


def migrate(source_id: str, next_version: int) -> dict:
    create_query = """mutation CreatePodMigration($input: MigratePodInput!) {
      migratePod(input: $input) {
        id sourcePodId targetPodId status message
      }
    }"""
    status_query = """query PodMigrationStatus($migrationId: String!) {
      podMigrationById(migrationId: $migrationId) {
        id status message sourcePodId targetPodId
      }
    }"""
    migration = graphql(create_query, {"input": {"podId": source_id}}).get("migratePod") or {}
    migration_id = migration.get("id")
    if not migration_id:
        raise RuntimeError("RunPod가 마이그레이션 ID를 반환하지 않았습니다")

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        info = graphql(status_query, {"migrationId": migration_id}).get("podMigrationById") or {}
        status = str(info.get("status", "UNKNOWN")).upper()
        if status == "COMPLETED":
            target_id = str(info.get("targetPodId") or "")
            if not target_id:
                raise RuntimeError("완료된 마이그레이션에 targetPodId가 없습니다")
            unlock(source_id)
            pod = wait_status(target_id, {"RUNNING"})
            new_name = f"{PREFIX}-v{next_version}"
            updated = request("PATCH", f"/v2/pods/{target_id}", {"name": new_name}) or {}
            pod.update(updated)
            pod["name"] = new_name
            wait_ready(target_id)
            pod_action(source_id, "terminate")
            print(f"마이그레이션 완료: {source_id} -> {target_id} ({new_name})")
            return pod
        if status in {"FAILED", "CANCELLED"}:
            raise RuntimeError(f"마이그레이션 {status}: {info.get('message', '')}")
        time.sleep(POLL_SECONDS)
    raise RuntimeError("마이그레이션 완료 대기 시간이 초과됐습니다")


def start() -> None:
    version, pod = current_pod()
    pod_id = str(pod["id"])
    if pod.get("status") == "RUNNING":
        wait_ready(pod_id)
        print(f"이미 실행 중입니다: {pod_id}")
        return
    try:
        pod_action(pod_id, "start")
        wait_status(pod_id, {"RUNNING"})
    except (ApiError, RuntimeError) as exc:
        print(f"기존 Pod 시작 실패, Auto Migration 시도: {exc}", file=sys.stderr)
        last_error = exc
        for attempt in range(1, 4):
            try:
                migrate(pod_id, version + 1)
                return
            except (ApiError, RuntimeError) as migration_error:
                last_error = migration_error
                unlock(pod_id)
                print(f"마이그레이션 {attempt}/3 실패: {migration_error}", file=sys.stderr)
                if attempt < 3:
                    time.sleep(POLL_SECONDS * attempt)
        raise RuntimeError(f"Auto Migration 3회 실패: {last_error}") from last_error
    # 컨테이너나 모델 준비 실패는 GPU 자리 부족을 뜻하지 않는다. 이미 실행된 Pod를
    # 마이그레이션하지 않고 외부 재시도 루프가 같은 Pod의 readiness를 다시 확인하게 한다.
    wait_ready(pod_id)
    print(f"시작 완료: {pod_id}")


def stop() -> None:
    _, pod = current_pod()
    pod_id = str(pod["id"])
    if pod.get("status") == "EXITED":
        print(f"이미 정지 상태입니다: {pod_id}")
        return
    pod_action(pod_id, "stop")
    wait_status(pod_id, {"EXITED"})
    print(f"정지 완료: {pod_id}")


if ACTION == "start":
    start()
elif ACTION == "stop":
    stop()
elif ACTION == "status":
    version, pod = current_pod()
    print(json.dumps({"version": version, "pod": pod}, ensure_ascii=False, indent=2))
else:
    raise SystemExit(f"지원하지 않는 작업입니다: {ACTION}")

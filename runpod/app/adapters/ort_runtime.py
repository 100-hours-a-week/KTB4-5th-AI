"""PaddleX HPI의 onnxruntime 백엔드를 메모리 아레나를 끈 pip onnxruntime 세션으로 교체한다.

왜 필요한가: PaddleX는 HPI onnxruntime 백엔드를 내장 C++ 런타임(ultra_infer, ORT 1.20.1)으로
실행하는데, 이 런타임은 스레드 수만 설정할 수 있고 CPU 메모리 아레나를 끌 방법이 없다. 아레나는
이미지 크기가 바뀔 때마다 더 큰 블록을 잡고 돌려주지 않아, 크기가 다른 영수증이 이어지면 RSS가
1GB → 8GB까지 늘다 OOM이 났다(2026-09-22 실험, 36장).

어떻게: PaddleX가 백엔드 런타임을 만드는 지점(HPInfer._build_ui_runtime) 하나만 가로채서,
backend가 "onnxruntime"일 때 같은 inference.onnx 파일로 pip onnxruntime InferenceSession을 만든다.
PaddleX 쪽은 런타임에 num_inputs()·get_input_info(i).name·infer(dict)만 요구하므로 그 셋만
구현한 어댑터를 돌려준다. 전처리·후처리(DB 후처리, CTC 디코딩 등)는 PaddleX 코드를 그대로 쓴다.

실측(같은 36장): 아레나 켬 1GB→8GB에서 OOM / 아레나 끔 0.9GB→1.7GB에서 멈춤, OCR 텍스트 36/36 동일.
속도는 스레드 4 기준 내장 런타임과 3% 이내.

위험과 대비: HPInfer._build_ui_runtime은 PaddleX 비공개 API다(paddlex 3.4.3에서 확인). 패치
설치에 실패하거나 세션 생성에 실패하면 경고 로그만 남기고 원래 경로(내장 런타임)로 돌아간다 —
그 경우 메모리는 다시 커지므로 PADDLE_ENGINE_RECYCLE_RSS_MB 안전장치가 받친다. PaddleX를
올리면 이 모듈이 로그에 "installed"를 남기는지 먼저 확인한다.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {"installed": False, "threads": 4}


class _OrtSessionRuntime:
    # onnx 파일 경로와 스레드 수를 받아 아레나를 끈 CPU 추론 세션을 만든다.
    def __init__(self, model_path: str, threads: int) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # 아레나를 끄면 요청마다 필요한 만큼만 malloc/free한다. 메모리 패턴 재사용도 입력 크기가
        # 매번 달라 이득이 없고 아레나 없이는 의미가 없어 같이 끈다.
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        # 추론이 끝난 스레드가 CPU를 붙잡고 바쁘게 기다리지 않게 한다 — 같은 서버에서 Gemma(Ollama)가
        # 동시에 돌기 때문에 CPU를 양보하는 쪽이 낫다(실측: 스핀 끔 88.6초 vs 켬 101.5초, 18장).
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._session = ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
        self._inputs = self._session.get_inputs()

    def num_inputs(self) -> int:
        return len(self._inputs)

    # PaddleX는 반환값의 .name만 쓴다. onnxruntime NodeArg가 같은 속성을 갖는다.
    def get_input_info(self, index: int) -> Any:
        return self._inputs[index]

    def infer(self, feed: dict[str, Any]) -> list[Any]:
        return self._session.run(None, feed)


# PaddleX HPI onnxruntime 백엔드를 아레나를 끈 세션으로 바꾸는 패치를 한 번만 설치한다.
# 설치에 성공하면 True, 실패하면(PaddleX 구조 변경, onnxruntime 미설치) 경고 후 False를 반환한다.
def install_no_arena_ort_backend(threads: int) -> bool:
    _state["threads"] = threads
    if _state["installed"]:
        return True
    try:
        import onnxruntime  # noqa: F401
        from paddlex.inference.models.common import static_infer

        hp_infer = static_infer.HPInfer
        original = hp_infer._build_ui_runtime
        get_model_paths = static_infer.get_model_paths
    except Exception:
        logger.warning("ocr.ort_no_arena.unavailable falling back to PaddleX built-in runtime", exc_info=True)
        return False

    def build_ui_runtime(self: Any, backend: str, backend_config: Any, ui_option: Any = None) -> Any:
        if backend != "onnxruntime":
            return original(self, backend, backend_config, ui_option)
        try:
            model_path = get_model_paths(self._model_dir, self.model_file_prefix)["onnx"]
            runtime = _OrtSessionRuntime(str(model_path), _state["threads"])
        except Exception:
            logger.warning("ocr.ort_no_arena.session_failed falling back to PaddleX built-in runtime", exc_info=True)
            return original(self, backend, backend_config, ui_option)
        logger.info("ocr.ort_no_arena.session_created model=%s threads=%s", model_path, _state["threads"])
        return runtime

    hp_infer._build_ui_runtime = build_ui_runtime
    _state["installed"] = True
    logger.info("ocr.ort_no_arena.installed threads=%s", threads)
    return True

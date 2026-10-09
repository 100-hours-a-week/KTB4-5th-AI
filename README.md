# 다먹자 AI 서비스

하나의 저장소 안에서 배포 대상을 두 폴더로 분리한다.

```text
KTB4-5th-AI/
├── fastapi/   # EC2에 배포하는 API·DB·정책 서버
└── runpod/    # RunPod에 배포하는 GPU 추론 Worker
```

- EC2에는 [`fastapi/`](fastapi/README.md) 폴더의 내용만 올린다.
- RunPod에는 [`runpod/`](runpod/README.md) 폴더의 내용만 올린다.
- 두 서버는 `POST /internal/v1/infer`로 통신하며 같은 Worker API 키를 사용한다.
- PostgreSQL, 분석 큐, 소비기한 정책과 최종 응답 생성은 EC2가 담당한다.
- MobileNetV3, PP-OCRv5, Gemma 이미지 추론은 RunPod가 담당한다.

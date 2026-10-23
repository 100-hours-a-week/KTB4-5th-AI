"""로컬 임시 DB에서만 정확/별칭/실제 pgvector 후보를 비교한다. 운영 추천은 변경하지 않는다."""

import argparse
import asyncio
import hashlib
import json
import resource
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.recipes.embeddings import DIMENSIONS, MODEL_NAME, MODEL_REVISION, TEXT_VERSION, RecipeEmbedder  # noqa: E402
from app.recipes.repository import RecipeRepository  # noqa: E402
from app.recipes.retrieval import allowed, canonical, coverage_score, reciprocal_rank_fusion, variants  # noqa: E402
from app.recipes.schemas import Recipe, normalize  # noqa: E402
from app.recipes.vector_repository import RecipeVectorRepository  # noqa: E402

CASES = [
    (["두부", "대파", "마늘"], []), (["감자", "양파", "당근"], []),
    (["달걀", "대파", "당근"], []), (["계란", "대파", "당근"], []),
    (["소고기", "무", "대파"], []), (["쇠고기", "무", "대파"], []),
    (["돼지고기", "양파", "마늘"], []), (["돈육", "양파", "마늘"], []),
    (["무우", "소고기"], []), (["브로컬리", "당근"], []),
    (["우유", "달걀", "밀가루"], []), (["두부", "김치", "돼지고기"], []),
    (["연두부", "새우", "달걀"], []), (["냉동닭가슴살", "양파"], []),
    (["두부", "당근"], ["땅콩"]), (["소고기", "감자"], ["쇠고기"]),
    (["우유"], ["우유"]), (["감자"], ["땅콩", "우유", "달걀"]),
    (["드라이버", "AA건전지"], []), (["아스팔트", "볼트"], []),
    (["두유"], ["우유"]), (["순두부"], []),
]


# 결과 수와 무관하게 이름·별칭 일치와 제외 조건을 독립적으로 검사해 비교 지표를 만든다.
def assess(rows: list[Recipe], inventory: list[str], excluded: list[str]) -> dict:
    wanted = {canonical(name) for name in inventory}
    matches = [allowed(recipe, inventory, excluded) for recipe in rows]
    return {"count": len(rows), "rulePrecisionAt5": sum(matches) / len(rows) if rows else None,
            "wholeInventoryCovered": sum(wanted.issubset({canonical(item.name) for item in recipe.ingredients}) for recipe in rows),
            "items": [{"sourceRecipeId": recipe.sourceRecipeId, "name": recipe.name,
                       "ingredients": [item.name for item in recipe.ingredients], "passesRuleCheck": matches[index]}
                      for index, recipe in enumerate(rows)]}


# 기존의 보유 비율·부족 감점에 따라 순위를 매긴다. 모든 재고는 동일하게 유효하다고 통제한다.
def lexical_rank(rows: list[Recipe], inventory: list[str], excluded: list[str], aliases: bool) -> list[Recipe]:
    transform = canonical if aliases else normalize
    names = {transform(name) for name in inventory}
    rows = [recipe for recipe in rows if allowed(recipe, inventory, excluded) and names.intersection(transform(item.name) for item in recipe.ingredients)]
    return sorted(rows, key=lambda recipe: (-coverage_score(recipe, inventory, aliases), recipe.source, recipe.sourceRecipeId))


# 이번에 생성한 전용 localhost DB만 허용하며 운영 DB 연결 문자열을 받지 않는다.
def validate_database_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgresql", "postgres"} or parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != 15438 or parsed.path != "/recipe_vector_bench":
        raise ValueError("localhost:15438/recipe_vector_bench 전용 임시 DB만 사용할 수 있습니다.")


# 지연 시간 요약을 반환한다. 작은 표본의 P95는 탐색 지표이며 운영 성능 보장이 아니다.
def timings(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"meanMs": round(statistics.mean(values), 3), "medianMs": round(statistics.median(values), 3),
            "p95Ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3), "maxMs": round(max(values), 3), "samples": len(values)}


# 실제 모델과 PostgreSQL을 사용해 비교하고 결과·벡터·보고서를 신규 출력 디렉터리에 저장한다.
async def run(args: argparse.Namespace) -> None:
    validate_database_url(args.database_url)
    source_bytes = args.input.read_bytes()
    recipes = [Recipe.model_validate_json(line) for line in source_bytes.decode().splitlines() if line.strip()]
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    print("STAGE load_embedding_model", flush=True)
    model_start = time.monotonic()
    embedder = RecipeEmbedder(str(args.output.parent / "model_cache"))
    load_seconds = time.monotonic() - model_start
    print("STAGE embed_documents", len(recipes), flush=True)
    embed_start = time.monotonic()
    embeddings = embedder.encode_recipes(recipes)
    document_seconds = time.monotonic() - embed_start
    import numpy as np

    np.save(args.output / "embeddings.npy", np.asarray(embeddings, dtype=np.float32))
    (args.output / "recipe_ids.json").write_text(json.dumps([recipe.sourceRecipeId for recipe in recipes]), encoding="utf-8")
    repository = RecipeRepository(Settings(_env_file=None, database_url=args.database_url))
    vectors = RecipeVectorRepository(repository)
    try:
        pool = await repository.pool.get()
        for migration in ("020_recipe_storage.sql", "021_recipe_embeddings.sql"):
            await pool.execute((Path(__file__).parent / "migrations" / migration).read_text())
        version = await pool.fetchval("SELECT extversion FROM pg_extension WHERE extname='vector'")
        postgres_version = await pool.fetchval("SHOW server_version")
        print("STAGE insert_isolated_database", flush=True)
        for recipe, vector in zip(recipes, embeddings, strict=True):
            await repository.save(recipe, {"benchmark": True}, "DEV_BENCHMARK_ONLY_NOT_FOR_REPUBLISH")
            await vectors.save(recipe, vector)
        # 실제 DB에서 오래된 원문 벡터와 비활성 레시피를 차단하는지 확인하고 원래 스냅샷으로 복원한다.
        probe = recipes[0]
        await repository.save(probe.model_copy(update={"sourceRevision": "benchmark-stale-revision"}), {}, "DEV_BENCHMARK_ONLY")
        stale_excluded = all(recipe.sourceRecipeId != probe.sourceRecipeId for recipe, _ in await vectors.nearest(embeddings[0], 500))
        await repository.save(probe, {}, "DEV_BENCHMARK_ONLY")
        await repository.save(probe.model_copy(update={"status": "INACTIVE"}), {}, "DEV_BENCHMARK_ONLY")
        inactive_excluded = all(recipe.sourceRecipeId != probe.sourceRecipeId for recipe, _ in await vectors.nearest(embeddings[0], 500))
        await repository.save(probe, {}, "DEV_BENCHMARK_ONLY")
        if not stale_excluded or not inactive_excluded:
            raise RuntimeError("원천 revision/비활성 후보 차단 검증 실패")
        check_rows = await vectors.nearest(embeddings[0], 5)
        id_to_index = {recipe.sourceRecipeId: index for index, recipe in enumerate(recipes)}
        array = np.asarray(embeddings)
        differences = [abs(similarity - float(array[id_to_index[recipe.sourceRecipeId]] @ array[0])) for recipe, similarity in check_rows]
        if max(differences) > 0.00001:
            raise RuntimeError("pgvector cosine 결과와 정규화 벡터 내적이 일치하지 않습니다.")
        measurements = {name: [] for name in ("exactLookupAndRank", "aliasLookupAndRank", "queryEmbedding", "vectorLookupAndParse", "hybridTotal")}
        cases = []
        print("STAGE evaluate_queries", len(CASES), flush=True)
        # 모델 최초 질의 워밍업을 통계에서 제외한다. 다운로드·문서 임베딩도 별도 기록한다.
        embedder.encode_query(["두부"])
        for case_id, (inventory, excluded) in enumerate(CASES, start=1):
            for _repeat in range(args.repeats):
                begin = time.monotonic()
                exact = lexical_rank(await repository.find([normalize(name) for name in inventory]), inventory, excluded, False)
                exact_ms = (time.monotonic() - begin) * 1000
                begin = time.monotonic()
                aliases = lexical_rank(await repository.find(variants(inventory)), inventory, excluded, True)
                alias_ms = (time.monotonic() - begin) * 1000
                begin = time.monotonic()
                query = embedder.encode_query(inventory)
                encoding_ms = (time.monotonic() - begin) * 1000
                begin = time.monotonic()
                semantic = [recipe for recipe, _ in await vectors.nearest(query, 50)]
                vector_ms = (time.monotonic() - begin) * 1000
                begin = time.monotonic()
                hybrid = reciprocal_rank_fusion(aliases[:50], semantic, inventory, excluded, 5)
                fusion_ms = (time.monotonic() - begin) * 1000
                for name, value in (("exactLookupAndRank", exact_ms), ("aliasLookupAndRank", alias_ms), ("queryEmbedding", encoding_ms),
                                    ("vectorLookupAndParse", vector_ms), ("hybridTotal", alias_ms + encoding_ms + vector_ms + fusion_ms)):
                    measurements[name].append(value)
            cases.append({"id": case_id, "inventory": inventory, "excluded": excluded, "exactEligible": len(exact), "aliasEligible": len(aliases),
                          "exact": assess(exact[:5], inventory, excluded), "aliases": assess(aliases[:5], inventory, excluded),
                          "vectorWithoutSafetyGate": assess(semantic[:5], inventory, excluded), "hybrid": assess(hybrid, inventory, excluded),
                          "vectorCandidatesRejectedByGate": sum(not allowed(recipe, inventory, excluded) for recipe in semantic),
                          "verifiedCandidatesNotInAliasSearch": sorted({recipe.sourceRecipeId for recipe in semantic if allowed(recipe, inventory, excluded)}
                                                                      - {recipe.sourceRecipeId for recipe in aliases})})
        summary = {"generatedAt": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), "datasetRows": len(recipes),
                   "datasetSha256": hashlib.sha256(source_bytes).hexdigest(), "model": MODEL_NAME, "modelRevision": MODEL_REVISION,
                   "textVersion": TEXT_VERSION, "dimensions": DIMENSIONS, "device": "cpu", "pgvectorVersion": version,
                   "postgresVersion": postgres_version, "caseCount": len(CASES), "repeats": args.repeats,
                   "integrationChecks": {"staleRevisionExcluded": stale_excluded, "inactiveExcluded": inactive_excluded, "maxCosineDifferenceVsNumpy": max(differences)},
                   "modelLoadIncludingDownloadSeconds": round(load_seconds, 3), "documentEmbeddingSeconds": round(document_seconds, 3),
                   "timings": {name: timings(values) for name, values in measurements.items()},
                   "peakProcessMemoryMiB": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if sys.platform == "darwin" else 1024), 2),
                   "elapsedSeconds": round(time.monotonic() - started, 3), "operationalDeploymentChanged": False,
                   "evaluationLabelType": "synthetic_inventory_queries_and_deterministic_name_alias_exclusion_rules_not_human_recipe_quality_labels"}
        summary["diagnostics"] = {
            "aliasExpandedCases": sum(case["aliasEligible"] > case["exactEligible"] for case in cases),
            "hybridNewVerifiedCandidatesBeyondAliases": sum(len(case["verifiedCandidatesNotInAliasSearch"]) for case in cases),
            "rawVectorRuleViolationsAt5": sum(not item["passesRuleCheck"] for case in cases for item in case["vectorWithoutSafetyGate"]["items"]),
            "rawVectorReturnedAt5": sum(case["vectorWithoutSafetyGate"]["count"] for case in cases),
            "hybridRuleViolationsAt5": sum(not item["passesRuleCheck"] for case in cases for item in case["hybrid"]["items"]),
        }
        (args.output / "results.json").write_text(json.dumps({"summary": summary, "cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# 레시피 검색 비교 실험", "", f"- 실제 pgvector {version}, PostgreSQL {postgres_version}, CPU", f"- 데이터 {len(recipes)}건, 질의 {len(CASES)}개, 반복 {args.repeats}회",
                 f"- 모델 {MODEL_NAME}, revision `{MODEL_REVISION}`", "- 운영 DB·FastAPI 추천 경로는 변경하지 않음.",
                 "- 사람이 매긴 추천 품질 정확도가 아니라 합성 재고 질의의 이름·별칭·제외 규칙을 검증한 탐색 실험.", "",
                 "| 구간 | 평균 ms | 중앙값 ms | P95 ms |", "| --- | ---: | ---: | ---: |"]
        for name, timing in summary["timings"].items():
            lines.append(f"| {name} | {timing['meanMs']} | {timing['medianMs']} | {timing['p95Ms']} |")
        lines += ["", f"문서 임베딩 {document_seconds:.3f}초, 모델 로드·다운로드 {load_seconds:.3f}초. 질의 지연과 구분한다.",
                  "", "| 질의 | 정확 일치 후보 | 별칭 후보 | 검증 후 hybrid 반환 | 검증 전 vector 규칙 위반(top50) |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for case in cases:
            lines.append(f"| {', '.join(case['inventory'])} / 제외 {', '.join(case['excluded']) or '없음'} | {case['exactEligible']} | {case['aliasEligible']} "
                         f"| {case['hybrid']['count']} | {case['vectorCandidatesRejectedByGate']} |")
        lines += ["", "## 제한", "", "- rulePrecisionAt5는 정답 기반 요리 추천 정확도가 아니다. 재료 일치·제외 규칙 충족 비율이다.",
                  "- 별칭을 포함한 이름 일치를 안전 게이트로 사용하므로 그 규칙 밖의 의미적 대체 재료를 허용하지 않는다.",
                  "- 재고 소비기한은 모두 유효하다고 통제했고 수량 충족·개인 선호·조리 맛·알레르기 안전은 평가하지 않았다.",
                  "- 같은 Mac에서 임시 Docker DB와 모델을 실행한 결과이며 EC2·RunPod 운영 지연 예측에 사용하지 않는다.",
                  "- 모델·문서 벡터를 사전 준비했으며 FastAPI 기본 실행에 임베딩 모델을 추가하지 않았다.",
                  "- Lookup 시간에는 DB 왕복·스냅샷 역직렬화가 포함되고 정확/별칭 방식에는 규칙 순위 계산도 포함된다. 순수 SQL 시간으로 해석하지 않는다.",
                  "- hybridTotal은 별칭 조회·질의 임베딩·벡터 조회·검증/병합을 순차 실행한 전체 시간이다.",
                  "- 이름·별칭 일치를 정답과 게이트로 공유하므로 별칭 검색 밖 의미적 이득을 이 지표로 증명할 수 없다. 별도 사람 평가가 필요하다."]
        (args.output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        print("REPORT", str(args.output / "report.md"), flush=True)
    finally:
        await repository.close()


# 입력 스냅샷과 임시 DB를 명시한 경우에만 실험을 실행한다. .env는 읽지 않는다.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3, choices=range(1, 11))
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

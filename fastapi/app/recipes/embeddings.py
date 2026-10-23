"""선택적 CPU 임베딩 생성기. 기본 FastAPI 실행 시 모델을 로드하거나 다운로드하지 않는다."""

from app.recipes.schemas import Recipe

MODEL_NAME = "intfloat/multilingual-e5-small"
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
TEXT_VERSION = "recipe-title-ingredients-v1"
DIMENSIONS = 384


# 제목과 원문 재료명만 임베딩한다. 수량·기한 검증을 벡터 검색에 맡기지 않는다.
def recipe_text(recipe: Recipe) -> str:
    return f"passage: 요리: {recipe.name}. 재료: " + ", ".join(item.name for item in recipe.ingredients)


class RecipeEmbedder:
    # 선택 의존성을 지연 import하고 고정 revision·safetensors·CPU로 공개 모델을 로드한다.
    def __init__(self, cache_folder: str | None = None) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(2)
        self.model = SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION, device="cpu", cache_folder=cache_folder,
                                         trust_remote_code=False, token=False, model_kwargs={"use_safetensors": True})
        self.model.max_seq_length = 512

    # 문서들을 L2 정규화한 384차원 벡터로 반환한다. 결과는 캐시 파일이나 별도 DB에 보관한다.
    def encode_recipes(self, recipes: list[Recipe]) -> list[list[float]]:
        return self.model.encode([recipe_text(recipe) for recipe in recipes], batch_size=16, normalize_embeddings=True, show_progress_bar=False).tolist()

    # 한 재고 질의를 같은 모델·차원·정규화 방식으로 변환한다.
    def encode_query(self, inventory: list[str]) -> list[float]:
        query = "query: " + ", ".join(inventory) + " 재료로 만드는 요리"
        return self.model.encode(query, normalize_embeddings=True, show_progress_bar=False).tolist()

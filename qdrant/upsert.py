from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
from sentence_transformers import SentenceTransformer
import uuid

QDRANT_URL = "http://localhost:6333"
COLLECTION = "recipes"

model = SentenceTransformer("BAAI/bge-small-en-v1.5")  # 384 dims
client = QdrantClient(url=QDRANT_URL)

def upsert_recipe(recipe_id: str, title: str, tags: list[str], url: str | None, chunks: list[str]):
    vectors = model.encode(chunks, normalize_embeddings=True)
    points = []
    for chunk, vec in zip(chunks, vectors):
        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vec.tolist(),
                payload={
                    "recipe_id": recipe_id,
                    "title": title,
                    "tags": tags,
                    "url": url,
                    "text": chunk,
                },
            )
        )
    client.upsert(collection_name=COLLECTION, points=points)

# Example usage
upsert_recipe(
    recipe_id="salmon_lemon_asparagus",
    title="Salmon Lemon Butter Asparagus",
    tags=["keto", "quick", "dinner"],
    url=None,
    chunks=[
        "Ingredients: salmon, butter, lemon, asparagus, dill, salt, pepper.",
        "Steps: Pan-sear salmon. Add butter and lemon. Toss asparagus until tender. Finish with dill.",
    ],
)

"""Phase 1: load CLIP, embed images, compare cosine similarities.

Success criterion: similar pairs score clearly higher than dissimilar pairs.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import torch
import torch.nn.functional as F
from dotenv import load_dotenv
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample_images"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase1")


def load_config() -> tuple[str, float]:
    load_dotenv(ROOT / ".env")
    model_name = os.getenv("CLIP_MODEL_NAME", "openai/clip-vit-base-patch32")
    threshold = float(os.getenv("SIMILARITY_THRESHOLD", "0.90"))
    return model_name, threshold


def load_clip(model_name: str) -> tuple[CLIPModel, CLIPProcessor, torch.device]:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info("loading model=%s device=%s", model_name, device)
    model = CLIPModel.from_pretrained(model_name)
    processor = CLIPProcessor.from_pretrained(model_name)
    model = model.to(device)
    model.eval()
    return model, processor, device


@torch.inference_mode()
def embed_image(
    model: CLIPModel,
    processor: CLIPProcessor,
    device: torch.device,
    image_path: Path,
) -> torch.Tensor:
    image = Image.open(image_path).convert("RGB")
    inputs = processor(images=image, return_tensors="pt")
    pixel_values = inputs["pixel_values"].to(device)
    # transformers>=5 returns BaseModelOutputWithPooling; pooler_output holds projected features
    outputs = model.get_image_features(pixel_values=pixel_values)
    features = outputs.pooler_output if hasattr(outputs, "pooler_output") else outputs
    vector = F.normalize(features, p=2, dim=-1).squeeze(0).cpu()
    return vector


def cosine_similarity(a: torch.Tensor, b: torch.Tensor) -> float:
    return float(torch.dot(a, b).item())


def main() -> int:
    model_name, threshold = load_config()
    log.info("SIMILARITY_THRESHOLD=%.3f", threshold)

    if not SAMPLE_DIR.exists() or not any(SAMPLE_DIR.glob("*.jpg")):
        log.error(
            "sample images missing under %s — run: python scripts/generate_samples.py",
            SAMPLE_DIR.as_posix(),
        )
        return 1

    model, processor, device = load_clip(model_name)

    names = [
        "sunset.jpg",
        "sunset_bright.jpg",
        "ocean.jpg",
        "ocean_crop.jpg",
        "forest.jpg",
        "city.jpg",
    ]
    vectors: dict[str, torch.Tensor] = {}
    for name in names:
        path = SAMPLE_DIR / name
        if not path.is_file():
            log.error("missing sample: %s", path.as_posix())
            return 1
        vec = embed_image(model, processor, device, path)
        vectors[name] = vec
        log.info("embedded %s dim=%d", name, vec.numel())

    similar_pairs = [
        ("sunset.jpg", "sunset_bright.jpg"),
        ("ocean.jpg", "ocean_crop.jpg"),
    ]
    dissimilar_pairs = [
        ("sunset.jpg", "ocean.jpg"),
        ("sunset.jpg", "forest.jpg"),
        ("sunset.jpg", "city.jpg"),
        ("ocean.jpg", "forest.jpg"),
        ("ocean.jpg", "city.jpg"),
        ("forest.jpg", "city.jpg"),
    ]

    print("\n=== Similar pairs ===")
    similar_scores: list[float] = []
    for left, right in similar_pairs:
        score = cosine_similarity(vectors[left], vectors[right])
        similar_scores.append(score)
        flag = "PASS" if score >= threshold else "below-threshold"
        print(f"  {left} <-> {right}: {score:.4f}  [{flag}]")

    print("\n=== Dissimilar pairs ===")
    dissimilar_scores: list[float] = []
    for left, right in dissimilar_pairs:
        score = cosine_similarity(vectors[left], vectors[right])
        dissimilar_scores.append(score)
        flag = "ok-gap" if score < threshold else "TOO-HIGH"
        print(f"  {left} <-> {right}: {score:.4f}  [{flag}]")

    min_similar = min(similar_scores)
    max_dissimilar = max(dissimilar_scores)
    gap = min_similar - max_dissimilar

    print("\n=== Summary ===")
    print(f"  min(similar)     = {min_similar:.4f}")
    print(f"  max(dissimilar)  = {max_dissimilar:.4f}")
    print(f"  gap              = {gap:.4f}")
    print(f"  threshold        = {threshold:.4f}")

    if gap <= 0:
        log.error("FAILED: no clear gap between similar and dissimilar pairs")
        return 2

    if min_similar < threshold <= max_dissimilar:
        log.warning(
            "gap exists but threshold=%.3f sits poorly; consider adjusting SIMILARITY_THRESHOLD",
            threshold,
        )
    elif min_similar < threshold:
        log.warning(
            "similar pairs fall below threshold=%.3f; lower SIMILARITY_THRESHOLD if needed",
            threshold,
        )
    else:
        log.info("threshold separates similar vs dissimilar cleanly")

    log.info("Phase 1 SUCCESS: clear similarity gap (%.4f)", gap)
    return 0


if __name__ == "__main__":
    sys.exit(main())

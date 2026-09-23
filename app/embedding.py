"""CLIP image embedding (L2-normalized 512-d vectors).

Load the model once at process start via ``ClipEmbedder.load()`` / ``get_embedder()``.
"""

from __future__ import annotations

import logging
import os
from io import BytesIO
from pathlib import Path

import torch
import torch.nn.functional as F
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError
from transformers import CLIPModel, CLIPProcessor

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VECTOR_SIZE = 512


class InvalidImageError(ValueError):
    """Raised when the uploaded bytes cannot be decoded as an RGB image."""


class ClipEmbedder:
    def __init__(self, model_name: str | None = None) -> None:
        load_dotenv(PROJECT_ROOT / ".env")
        self.model_name = model_name or os.getenv(
            "CLIP_MODEL_NAME", "openai/clip-vit-base-patch32"
        )
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._model: CLIPModel | None = None
        self._processor: CLIPProcessor | None = None

    def load(self) -> None:
        if self._model is not None:
            return
        log.info("loading CLIP model=%s device=%s", self.model_name, self.device)
        model = CLIPModel.from_pretrained(self.model_name)
        processor = CLIPProcessor.from_pretrained(self.model_name)
        model = model.to(self.device)
        model.eval()
        self._model = model
        self._processor = processor
        log.info("CLIP ready")

    @property
    def model(self) -> CLIPModel:
        self.load()
        assert self._model is not None
        return self._model

    @property
    def processor(self) -> CLIPProcessor:
        self.load()
        assert self._processor is not None
        return self._processor

    def _open_rgb(self, data: bytes) -> Image.Image:
        try:
            image = Image.open(BytesIO(data))
            image.load()
            return image.convert("RGB")
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidImageError(f"invalid or unreadable image: {exc}") from exc

    @torch.inference_mode()
    def extract(self, data: bytes) -> list[float]:
        """Embed raw image bytes → L2-normalized 512-d vector."""
        image = self._open_rgb(data)
        inputs = self.processor(images=image, return_tensors="pt")
        pixel_values = inputs["pixel_values"].to(self.device)
        # transformers>=5 returns BaseModelOutputWithPooling; use pooler_output
        outputs = self.model.get_image_features(pixel_values=pixel_values)
        features = outputs.pooler_output if hasattr(outputs, "pooler_output") else outputs
        vector = F.normalize(features, p=2, dim=-1).squeeze(0).cpu()
        if vector.numel() != VECTOR_SIZE:
            raise RuntimeError(
                f"unexpected embedding size {vector.numel()}, expected {VECTOR_SIZE}"
            )
        return vector.tolist()

    def extract_path(self, path: Path) -> list[float]:
        return self.extract(path.read_bytes())


_embedder: ClipEmbedder | None = None


def get_embedder() -> ClipEmbedder:
    global _embedder
    if _embedder is None:
        _embedder = ClipEmbedder()
    return _embedder


def extract(data: bytes) -> list[float]:
    """Module-level helper used by the API and scripts."""
    return get_embedder().extract(data)

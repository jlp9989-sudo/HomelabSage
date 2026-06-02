"""Image-size growth detector — concrete signal for "bloatware" updates.

What counts as bloatware is famously hard to define, so we pin it to a
single, measurable thing: image size. When the candidate version's image
weighs significantly more than the current one, flag it. The analyzer's
prompt rule then asks the LLM to mention the growth in `summary` and
include it among `breaking_changes` if the user runs on constrained
storage.

We intentionally do NOT try to inspect what the new bytes are (entrypoint
diff, dependency bloat, embedded data) — that would mean shelling into
`skopeo` / `crane` and parsing manifests, an order of magnitude more code
for marginal extra signal. The headline number is enough to surface the
"this used to be 80 MB and is now 600 MB" case without false positives on
sub-2x growth that's part of normal lifecycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .registries import dockerhub_tag_info, parse_image_ref

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageSizeGrowth:
    """Result of a size-growth probe, ready to drop into `Update.context`.

    The dataclass surface intentionally mirrors what the LLM prompt rule
    will quote: bytes for the math, MiB for human readability, ratio for
    the threshold gate.
    """

    current_bytes: int
    new_bytes: int
    ratio: float
    triggered: bool

    def to_context(self) -> dict[str, Any]:
        return {
            "current_mib": round(self.current_bytes / 1024 / 1024, 1),
            "new_mib": round(self.new_bytes / 1024 / 1024, 1),
            "ratio": round(self.ratio, 2),
            "triggered": self.triggered,
        }


async def detect_growth(
    image_ref: str,
    current_size_bytes: int,
    new_tag: str,
    *,
    threshold_ratio: float = 2.0,
) -> ImageSizeGrowth | None:
    """Compare the local image's size with what the registry serves for `new_tag`.

    Returns:
      - `ImageSizeGrowth(triggered=True, …)` when the ratio meets/exceeds
        the threshold,
      - `ImageSizeGrowth(triggered=False, …)` when sizes are known but the
        growth is below threshold (still useful as context for the LLM),
      - `None` when we couldn't measure (non-Docker-Hub host, missing
        local size, registry didn't return a size).
    """
    if current_size_bytes <= 0:
        return None
    if threshold_ratio <= 0:
        return None
    ref = parse_image_ref(image_ref)
    if ref is None or not ref.is_docker_hub:
        # Other registries: skip until we add per-host probes. The OCI v2
        # manifest endpoint would work but needs auth in some cases.
        return None

    try:
        info = await dockerhub_tag_info(ref.slug, new_tag)
    except Exception as e:
        log.debug("image_size: dockerhub_tag_info failed for %s:%s — %s",
                  ref.slug, new_tag, e)
        return None
    if info is None or info.total_size_bytes <= 0:
        return None

    ratio = info.total_size_bytes / current_size_bytes
    return ImageSizeGrowth(
        current_bytes=current_size_bytes,
        new_bytes=info.total_size_bytes,
        ratio=ratio,
        triggered=ratio >= threshold_ratio,
    )

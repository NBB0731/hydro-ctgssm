"""Likelihoods and training objectives."""

from __future__ import annotations

import math

import torch
from torch import Tensor


def censored_gaussian_nll_elementwise(
    location: Tensor,
    scale: Tensor,
    value_or_limit: Tensor,
    observed: Tensor,
    censor: Tensor,
    target_weight: Tensor | None = None,
) -> tuple[Tensor, Tensor]:
    """Gaussian Tobit likelihood for exact and detection-limit observations.

    censor=-1 means value < reported limit; +1 means value > reported limit;
    censor=0 is an exact measurement. Inputs should already be transformed and
    standardized using training-set statistics.
    """
    scale = scale.clamp_min(1e-5)
    z = (value_or_limit - location) / scale
    exact_nll = 0.5 * z.square() + scale.log() + 0.5 * math.log(2.0 * math.pi)
    log_cdf = torch.special.log_ndtr(z)
    log_survival = torch.special.log_ndtr(-z)
    nll = torch.where(censor < 0, -log_cdf, torch.where(censor > 0, -log_survival, exact_nll))
    weight = observed.to(nll.dtype)
    if target_weight is not None:
        weight = weight * target_weight.view(*([1] * (weight.ndim - 1)), -1)
    return nll, weight


def censored_gaussian_nll(
    location: Tensor,
    scale: Tensor,
    value_or_limit: Tensor,
    observed: Tensor,
    censor: Tensor,
    target_weight: Tensor | None = None,
) -> Tensor:
    """Mean Gaussian Tobit loss over observed target entries."""
    nll, weight = censored_gaussian_nll_elementwise(
        location, scale, value_or_limit, observed, censor, target_weight
    )
    return (nll * weight).sum() / weight.sum().clamp_min(1.0)


class GroupDRO:
    """Exponentiated-gradient weights for basin/country robust training."""

    def __init__(self, n_groups: int, step_size: float = 0.05, device: str | torch.device = "cpu"):
        self.weights = torch.ones(n_groups, device=device) / n_groups
        self.step_size = step_size

    @torch.no_grad()
    def update(self, detached_group_losses: Tensor) -> None:
        self.weights *= torch.exp(self.step_size * detached_group_losses)
        self.weights /= self.weights.sum().clamp_min(1e-12)

    def aggregate(self, group_losses: Tensor) -> Tensor:
        return (self.weights * group_losses).sum()

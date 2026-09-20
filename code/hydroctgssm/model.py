"""Hydro-CTGSSM: data-driven architecture for sparse GEMStat river observations.

The model encodes each station's irregular event history, passes information
only along directed HydroRIVERS edges, and predicts a calibrated distribution
for multiple water-quality variables at a common anchor date.
"""

from __future__ import annotations

import torch
from torch import Tensor, nn
import torch.nn.functional as F


class IrregularStationEncoder(nn.Module):
    def __init__(self, n_targets: int, dynamic_dim: int, static_dim: int, hidden_dim: int, use_decay: bool = True):
        super().__init__()
        event_dim = n_targets * 3 + dynamic_dim + static_dim + 1
        self.decay = nn.Sequential(nn.Linear(1, hidden_dim), nn.Softplus())
        self.event = nn.Sequential(
            nn.Linear(event_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.SiLU(),
        )
        self.cell = nn.GRUCell(hidden_dim, hidden_dim)
        self.hidden_dim = hidden_dim
        self.use_decay = use_decay

    def forward(
        self,
        values: Tensor,
        observed: Tensor,
        censor: Tensor,
        delta_days: Tensor,
        dynamic: Tensor,
        static: Tensor,
        valid_event: Tensor,
    ) -> Tensor:
        """Encode padded histories.

        Shapes: values/observed/censor [N,L,K], delta_days/valid_event [N,L],
        dynamic [N,L,D], static [N,S]. Censor is -1/0/+1.
        """
        n_nodes, length, _ = values.shape
        h = values.new_zeros((n_nodes, self.hidden_dim))
        static_steps = static.unsqueeze(1).expand(-1, length, -1)
        event_input = torch.cat(
            [
                torch.nan_to_num(values) * observed,
                observed,
                censor,
                dynamic,
                static_steps,
                torch.log1p(delta_days).unsqueeze(-1),
            ],
            dim=-1,
        )
        embedded = self.event(event_input)
        for t in range(length):
            dt = delta_days[:, t : t + 1].clamp_min(0.0)
            h_prior = h * torch.exp(-self.decay(torch.log1p(dt)) * dt.sqrt()) if self.use_decay else h
            h_new = self.cell(embedded[:, t], h_prior)
            valid = valid_event[:, t : t + 1].to(h.dtype)
            h = valid * h_new + (1.0 - valid) * h
        return h


class DirectedHydroConv(nn.Module):
    def __init__(self, hidden_dim: int, edge_dim: int, dropout: float):
        super().__init__()
        self.message = nn.Linear(hidden_dim, hidden_dim, bias=False)
        self.edge_gate = nn.Sequential(
            nn.Linear(edge_dim, hidden_dim), nn.SiLU(), nn.Linear(hidden_dim, hidden_dim), nn.Sigmoid()
        )
        self.update = nn.GRUCell(hidden_dim, hidden_dim)
        self.norm = nn.LayerNorm(hidden_dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, h: Tensor, edge_index: Tensor, edge_attr: Tensor) -> Tensor:
        if edge_index.numel() == 0:
            return h
        source, downstream = edge_index
        msg = (self.message(h[source]) * self.edge_gate(edge_attr)).to(h.dtype)
        aggregate = torch.zeros_like(h)
        aggregate.index_add_(0, downstream, msg)
        degree = torch.zeros((h.size(0), 1), device=h.device, dtype=h.dtype)
        degree.index_add_(0, downstream, torch.ones((downstream.numel(), 1), device=h.device, dtype=h.dtype))
        aggregate = aggregate / degree.clamp_min(1.0)
        updated = self.update(self.dropout(aggregate), h)
        return self.norm(updated + h)


class HydroCTGSSM(nn.Module):
    def __init__(
        self,
        n_targets: int,
        dynamic_dim: int,
        static_dim: int,
        edge_dim: int,
        hidden_dim: int = 128,
        graph_layers: int = 3,
        dropout: float = 0.1,
        use_decay: bool = True,
    ):
        super().__init__()
        self.encoder = IrregularStationEncoder(n_targets, dynamic_dim, static_dim, hidden_dim, use_decay=use_decay)
        self.graph = nn.ModuleList(
            [DirectedHydroConv(hidden_dim, edge_dim, dropout) for _ in range(graph_layers)]
        )
        self.head = nn.Sequential(
            nn.Linear(hidden_dim + static_dim, hidden_dim),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, n_targets * 2),
        )
        self.n_targets = n_targets

    def forward(
        self,
        values: Tensor,
        observed: Tensor,
        censor: Tensor,
        delta_days: Tensor,
        dynamic: Tensor,
        static: Tensor,
        valid_event: Tensor,
        edge_index: Tensor,
        edge_attr: Tensor,
    ) -> dict[str, Tensor]:
        h = self.encoder(values, observed, censor, delta_days, dynamic, static, valid_event)
        for layer in self.graph:
            h = layer(h, edge_index, edge_attr)
        raw = self.head(torch.cat([h, static], dim=-1))
        location, raw_scale = raw.chunk(2, dim=-1)
        scale = F.softplus(raw_scale) + 1e-4
        return {"location": location, "scale": scale, "state": h}
